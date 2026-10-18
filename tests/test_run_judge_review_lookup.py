"""The judge's duplicate-review lookup must paginate and must authenticate the author.

Both defects were found on `Manolii-org/master` (PR #3486) in the sibling implementation
`scripts/assessment/judge.py`, and this pack ships its own older copy that never received
the fixes:

* **Unpaginated.** The reviews endpoint returns 30 per page, oldest-first. Any PR with
  more than 30 reviews stopped matching its own earlier verdict, so a re-run posted a
  duplicate. Master hit exactly this on its auto-merge gate (#3397): 44 reviews, 30
  returned, verdict stranded on page 2.
* **Marker-only.** The `<!-- pr-assessment-v1 -->` marker is public text — anyone who can
  review the PR can paste it, and a PR that discusses this file contains it. An unrelated
  review carrying it suppressed the genuine judge verdict, which fails silently: the
  assessment reports success and posts nothing.

`scripts/ci/judge_fallback.py` in master already required author + marker for this reason.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_judge_module():
    """run-judge.py is not importable by name (hyphen), so load it by path."""
    path = REPO_ROOT / "scripts" / "run-judge.py"
    spec = importlib.util.spec_from_file_location("run_judge_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_judge_under_test"] = module
    spec.loader.exec_module(module)
    return module


rj = _load_judge_module()

SHA = "1f9133dd479dad162586b0c43cf59656a613173d"

# The fixture sets no PR_TITLE/PR_BODY/PR_BASE_SHA and merge_danger is empty, so
# the judge's meta_digest is the hash of the empty six-field tuple. Embed this
# marker in reviews that represent a verdict for the current metadata; omit it to
# model a stale or marker-only review.
JUDGE_META = hashlib.sha256("\0\0\0\0".encode()).hexdigest()[:12]
MARKED = f"{rj.REVIEW_MARKER}\n<!-- meta:{JUDGE_META}:findings -->"


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _review(body: str, commit_id: str = SHA, author: str = rj.JUDGE_REVIEW_AUTHOR) -> dict:
    """A reviews-endpoint entry. Author defaults to the judge; override to forge one."""
    return {"commit_id": commit_id, "body": body, "user": {"login": author}}


def _paged(pages: list[list[dict]]):
    """Serve `pages` in order, honouring the ?page= query the caller sends."""

    def fake_urlopen(req, *, timeout=None, host=None):
        page = int(req.full_url.split("page=")[-1])
        body = pages[page - 1] if page - 1 < len(pages) else []
        return _FakeResponse(json.dumps(body).encode())

    return fake_urlopen


@pytest.fixture()
def judge(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "Manolii-org/ai-starter-pack")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    return rj.Judge(candidates_dir=".ai/candidates", pr_number=1, sha=SHA)


def test_finds_the_verdict_stranded_on_page_two(judge, monkeypatch):
    """The #3397 bug: unpaginated, page 2 is never fetched and a duplicate is posted."""
    page_one = [_review(rj.REVIEW_MARKER, commit_id="other")] * rj._REVIEWS_PER_PAGE
    page_two = [_review(MARKED + "\n## PR Assessment Review")]
    monkeypatch.setattr(rj, "_urlopen_https", _paged([page_one, page_two]))
    assert judge._review_exists_at_sha("findings") is True, "page 2 was never fetched"


def test_ignores_a_review_forged_by_another_author(judge, monkeypatch):
    """The marker is public text; only the author identity is unforgeable.

    Treating a non-judge review as the judge's own suppresses the real verdict — a
    silent failure, since the run still reports success.
    """
    forged = [_review(rj.REVIEW_MARKER, author="some-collaborator")]
    monkeypatch.setattr(rj, "_urlopen_https", _paged([forged]))
    assert judge._review_exists_at_sha("findings") is False, "a non-judge review was accepted"


def test_still_matches_a_genuine_judge_review(judge, monkeypatch):
    """The author filter must not break the idempotency it is guarding."""
    page = [_review(MARKED + "\n## PR Assessment Review")]
    monkeypatch.setattr(rj, "_urlopen_https", _paged([page]))
    assert judge._review_exists_at_sha("findings") is True


def test_marker_only_review_does_not_suppress(judge, monkeypatch):
    """A pre-meta-marker review carries no metadata digest, so it cannot prove the
    assessment is current — the judge must repost rather than stay silent."""
    page = [_review(rj.REVIEW_MARKER + "\n## PR Assessment Review")]
    monkeypatch.setattr(rj, "_urlopen_https", _paged([page]))
    assert judge._review_exists_at_sha("findings") is False


def test_latest_review_decides_aba(judge, monkeypatch):
    """A→B→A: metadata returns to A after B was assessed. The old A review must not
    suppress a fresh verdict — only the LATEST judge review's digest counts."""
    old_a = _review(MARKED + "\n## PR Assessment Review")
    newer_b = _review(f"{rj.REVIEW_MARKER}\n<!-- meta:otherdigest1 -->")
    monkeypatch.setattr(rj, "_urlopen_https", _paged([[old_a, newer_b]]))
    assert judge._review_exists_at_sha("findings") is False, "stale A review suppressed the re-run"


def test_latest_review_decides_aba_across_pages(judge, monkeypatch):
    """Same A→B→A but the stale A match sits on page 1 and the newer B verdict on
    page 2 — an early return on the page-1 match would suppress the re-run."""
    page_one = (
        [_review(rj.REVIEW_MARKER, commit_id="other")] * (rj._REVIEWS_PER_PAGE - 1)
        + [_review(MARKED + "\n## PR Assessment Review")]
    )
    page_two = [_review(f"{rj.REVIEW_MARKER}\n<!-- meta:otherdigest1 -->")]
    monkeypatch.setattr(rj, "_urlopen_https", _paged([page_one, page_two]))
    assert judge._review_exists_at_sha("findings") is False, "cross-page stale digest suppressed the re-run"


def test_ignores_a_review_on_a_different_sha(judge, monkeypatch):
    page = [_review(rj.REVIEW_MARKER, commit_id="deadbeef")]
    monkeypatch.setattr(rj, "_urlopen_https", _paged([page]))
    assert judge._review_exists_at_sha("findings") is False


def test_api_error_fails_open(judge, monkeypatch):
    """Unchanged behaviour: a lookup failure must not block the verdict from posting."""

    def boom(req, *, timeout=None, host=None):
        raise OSError("network down")

    monkeypatch.setattr(rj, "_urlopen_https", boom)
    assert judge._review_exists_at_sha("findings") is False


def test_malformed_response_fails_open(judge, monkeypatch):
    """A non-list payload must not be iterated as if it were reviews."""

    def not_a_list(req, *, timeout=None, host=None):
        return _FakeResponse(json.dumps({"message": "Not Found"}).encode())

    monkeypatch.setattr(rj, "_urlopen_https", not_a_list)
    assert judge._review_exists_at_sha("findings") is False


def test_page_cap_is_bounded(judge, monkeypatch):
    """A server that never returns a short page must not spin the loop forever."""
    calls = {"n": 0}

    def always_full(req, *, timeout=None, host=None):
        calls["n"] += 1
        # Always a full page, never the SHA — the only exit is the page cap.
        page = [_review(rj.REVIEW_MARKER, commit_id="other")] * rj._REVIEWS_PER_PAGE
        return _FakeResponse(json.dumps(page).encode())

    monkeypatch.setattr(rj, "_urlopen_https", always_full)
    assert judge._review_exists_at_sha("findings") is False
    assert calls["n"] == rj._MAX_REVIEW_PAGES, (
        f"expected the walk to stop at the {rj._MAX_REVIEW_PAGES}-page cap, "
        f"got {calls['n']} requests"
    )


@pytest.fixture
def invocation_context(judge, monkeypatch, tmp_path):
    """Supply the immutable source required by the evidence-grounded protocol."""
    subject = tmp_path / "subject"
    subject.mkdir()
    (subject / "file.py").write_text("def allowed(): return True\n")
    subprocess.run(["git", "init", "-q", str(subject)], check=True)
    subprocess.run(["git", "-C", str(subject), "add", "."], check=True)
    subprocess.run(["git", "-C", str(subject), "-c", "user.name=Offline",
                    "-c", "user.email=offline@example.invalid", "commit", "-qm", "Source fixture"], check=True)
    judge.sha = subprocess.check_output(["git", "-C", str(subject), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(subject))
    monkeypatch.setenv("PR_BASE_SHA", judge.sha)
    return rj.Finding("review", "file.py", 1, "WARNING", "Check", "Change the check in file.py:1")


@pytest.mark.parametrize(
    "proxied,responses,expected_calls,accepted",
    [
        (True, [("partial", "max_tokens"), ("valid", "end_turn")], 2, True),
        (True, [("valid", "max_tokens"), ("valid", "end_turn")], 2, True),
        (True, [("valid", "end_turn")], 1, True),
        (True, [("partial", "max_tokens"), ("valid", "max_tokens")], 2, False),
        (False, [("valid", "max_tokens")], 1, False),
        (False, [("valid", "end_turn")], 1, True),
        (True, [("partial", "end_turn")], 1, False),
        (True, [("empty", "max_tokens"), ("valid", "end_turn")], 2, True),
    ],
)
def test_judge_output_limit_uses_only_existing_retry(
    judge, monkeypatch, caplog, invocation_context, proxied, responses, expected_calls, accepted
):
    """Actual runner refuses even valid JSON with an incomplete API status."""
    endpoint = "https://proxy.example.invalid/v1/messages" if proxied else "https://api.anthropic.com/v1/messages"
    monkeypatch.setattr(rj, "_endpoint", lambda **kwargs: ("offline-placeholder", endpoint, proxied))
    verdict = {"surviving": [], "dropped_count": 1, "review_action": "COMMENT"}
    response_verdict = {"version": 2, "decisions": [{
        "finding_id": invocation_context.finding_id, "decision": "drop", "gate": "accuracy",
        "reason": "The supplied file.py:1 defines this intended behavior.",
        "evidence_ids": [hashlib.sha256(b"file.py|1").hexdigest()[:16]],
    }], "investigations": []}
    text = {"valid": json.dumps(response_verdict), "partial": "PRIVATE_PARTIAL_VERDICT{", "empty": ""}
    requests = []

    def respond(req, *, timeout=None, host=None):
        requests.append(json.loads(req.data))
        kind, stop_reason = responses[len(requests) - 1]
        assert timeout == 120
        assert req.full_url == endpoint
        return _FakeResponse(json.dumps({
            "content": [{"type": "text", "text": text[kind]}],
            "stop_reason": stop_reason,
        }).encode())

    monkeypatch.setattr(rj, "_urlopen_https", respond)
    with caplog.at_level("INFO"):
        result = judge.invoke_judge_agent([invocation_context])
    assert ({key: result[key] for key in verdict} if result is not None else None) == (verdict if accepted else None)
    if accepted:
        assert result["complete"] is True and result["decisions"] == response_verdict["decisions"]
    assert len(requests) == expected_calls
    budgets = [4000, 6048] if proxied else [4000]
    assert [request["max_tokens"] for request in requests] == budgets[:expected_calls]
    assert {request["model"] for request in requests} == {"sonnet" if proxied else "claude-sonnet-4-6"}
    if len(requests) == 2:
        assert requests[0] | {"max_tokens": requests[1]["max_tokens"]} == requests[1]
    assert "PRIVATE_PARTIAL_VERDICT" not in caplog.text
    assert json.dumps(verdict) not in caplog.text


@pytest.mark.parametrize("error", [TimeoutError("unknown remote outcome"), ValueError("malformed response")])
def test_judge_transport_failure_is_not_replayed(judge, monkeypatch, invocation_context, error):
    monkeypatch.setattr(rj, "_endpoint", lambda **kwargs: ("offline-placeholder", "https://proxy.example.invalid/v1/messages", True))
    requests = []

    def fail(req, *, timeout=None, host=None):
        requests.append(req)
        raise error

    monkeypatch.setattr(rj, "_urlopen_https", fail)
    assert judge.invoke_judge_agent([invocation_context]) is None
    assert len(requests) == 1


def _review_full(
    body: str,
    commit_id: str = SHA,
    author: str = rj.JUDGE_REVIEW_AUTHOR,
    state: str = "COMMENTED",
    review_id: int = 7001,
) -> dict:
    r = _review(body, commit_id=commit_id, author=author)
    r["state"] = state
    r["id"] = review_id
    return r


def test_clean_reassessment_dismisses_prior_request_changes(judge, monkeypatch):
    """A clean rerun at the same SHA must retire the blocking verdict — a
    COMMENT cannot supersede a REQUEST_CHANGES left active on the PR."""
    calls = {"dismissals": [], "posts": 0}

    blocking = _review_full(
        f"{rj.REVIEW_MARKER}\n<!-- meta:olderdigest:findings -->",
        state="CHANGES_REQUESTED",
    )

    def fake_urlopen(req, *, timeout=None, host=None):
        if req.get_method() == "GET":
            return _FakeResponse(json.dumps([blocking]).encode())
        if req.full_url.endswith("/dismissals"):
            assert req.get_method() == "PUT", "GitHub dismissals require PUT"
            calls["dismissals"].append(req.full_url)
            return _FakeResponse(b"{}")
        calls["posts"] += 1
        # Real POST /reviews returns the created review — its id is the live
        # verdict the keep-newest dismissal must preserve even when the list
        # endpoint (below) has not yet caught up (Codex P1 on #150).
        return _FakeResponse(b'{"id": 7002}')

    monkeypatch.setattr(rj, "_urlopen_https", fake_urlopen)
    judge._post_no_findings_comment()
    assert calls["posts"] == 1, "clean comment was not posted"
    assert len(calls["dismissals"]) == 1, "blocking review was not dismissed"
    assert "7001" in calls["dismissals"][0]


def test_clean_post_survives_dismissal_failure(judge, monkeypatch):
    """Dismissal is best-effort: a 422/404 on the dismissal endpoint must not
    block the clean comment itself."""
    blocking = _review_full(rj.REVIEW_MARKER, state="CHANGES_REQUESTED")

    def fake_urlopen(req, *, timeout=None, host=None):
        if req.get_method() == "GET":
            return _FakeResponse(json.dumps([blocking]).encode())
        if req.full_url.endswith("/dismissals"):
            raise OSError("dismissal denied")
        return _FakeResponse(b"{}")

    monkeypatch.setattr(rj, "_urlopen_https", fake_urlopen)
    judge._post_no_findings_comment()  # must not raise


def test_commented_or_approved_reviews_are_not_dismissed(judge, monkeypatch):
    """GitHub 422s on dismissing COMMENTED reviews, and a clean result does not
    contradict an APPROVED — neither must be touched."""
    calls = {"dismissals": 0}
    page = [
        _review_full(rj.REVIEW_MARKER, state="COMMENTED", review_id=1),
        _review_full(rj.REVIEW_MARKER, state="APPROVED", review_id=2),
        _review_full(rj.REVIEW_MARKER, state="DISMISSED", review_id=3),
        _review_full(rj.REVIEW_MARKER, state="CHANGES_REQUESTED", author="other-bot", review_id=4),
    ]

    def fake_urlopen(req, *, timeout=None, host=None):
        if req.get_method() == "GET":
            return _FakeResponse(json.dumps(page).encode())
        if req.full_url.endswith("/dismissals"):
            calls["dismissals"] += 1
        return _FakeResponse(b"{}")

    monkeypatch.setattr(rj, "_urlopen_https", fake_urlopen)
    judge._post_no_findings_comment()
    assert calls["dismissals"] == 0


def test_marker_text_appears_exactly_once_in_the_source():
    """Every posting path must build its body from REVIEW_MARKER, not a literal.

    Counting the RAW marker text, not a quoted literal: the earlier form
    `'"<!-- pr-assessment-v1 -->"'` missed `"<!-- pr-assessment-v1 -->\\n"` in
    _post_no_findings_comment and _post_advisory_warning, so it passed while two
    hardcoded copies survived. A stale copy is not cosmetic — the lookup matches on
    REVIEW_MARKER, so a drifted posting path posts reviews the dedup cannot see, and
    every re-run adds another.
    """
    source = (REPO_ROOT / "scripts" / "run-judge.py").read_text()
    assert source.count("<!-- pr-assessment-v1 -->") == 1, (
        "the marker text must appear once, as REVIEW_MARKER — "
        "every other site should interpolate the constant"
    )


@pytest.mark.parametrize("acknowledgement", ["posted", "failed", "empty", "malformed_id"])
@pytest.mark.parametrize("failure", ["invocation", "specificity", "adjudication"])
def test_failed_judge_retires_only_stale_approval_after_advisory_ack(
    judge, monkeypatch, tmp_path, caplog, acknowledgement, failure
):
    """Exercise run, real advisory posting and authenticated dismissal together."""
    judge.candidates_dir = tmp_path / "candidates"
    judge.candidates_dir.mkdir()
    judge.judge_log_dir = tmp_path / "logs"
    (judge.candidates_dir / "review.json").write_text(json.dumps({
        "source": "review", "findings": [{
            "file": "policy.py", "line": 3, "severity": "ERROR",
            "message": "Missing tenant guard", "fix": "" if failure == "specificity" else "Add tenant guard at policy.py:3",
        }],
    }))
    monkeypatch.setattr(judge, "invoke_judge_agent", lambda findings: (
        {"surviving": [], "dropped_count": 1, "complete": False, "decisions": []}
        if failure == "adjudication" else None
    ))
    reviews = [
        _review_full(rj.REVIEW_MARKER, state="APPROVED", review_id=1),
        _review_full(rj.REVIEW_MARKER, state="CHANGES_REQUESTED", review_id=2),
        _review_full(rj.REVIEW_MARKER, state="APPROVED", author="other-bot", review_id=3),
        _review_full(rj.REVIEW_MARKER, state="APPROVED", commit_id="other-sha", review_id=4),
    ]
    calls = []

    def serve(req, *, timeout=None, host=None):
        calls.append((req.get_method(), req.full_url))
        if req.get_method() == "POST":
            assert json.loads(req.data)["event"] == "COMMENT"
            if acknowledgement == "failed":
                raise OSError("advisory outcome unknown")
            body = {"id": 5} if acknowledgement == "posted" else ({"id": "5"} if acknowledgement == "malformed_id" else {})
            return _FakeResponse(json.dumps(body).encode())
        if req.get_method() == "GET":
            return _FakeResponse(json.dumps(reviews).encode())
        assert req.get_method() == "PUT"
        return _FakeResponse(b"{}")

    monkeypatch.setattr(rj, "_urlopen_https", serve)
    with caplog.at_level("INFO"):
        assert judge.run() == 0  # Advisory exit is not a successful model verdict.
    assert ("Advisory warning posted" in caplog.text) is (acknowledgement == "posted")
    assert [method for method, _ in calls].count("POST") == 1
    dismissed = [url for method, url in calls if method == "PUT"]
    assert dismissed == (["https://api.github.com/repos/Manolii-org/ai-starter-pack/pulls/1/reviews/1/dismissals"] if acknowledgement == "posted" else [])
    if acknowledgement != "posted":
        assert [method for method, _ in calls] == ["POST"]
