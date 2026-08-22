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

import importlib.util
import json
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

    def fake_urlopen(req, timeout=None):
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
    page_two = [_review(rj.REVIEW_MARKER + "\n## PR Assessment Review")]
    monkeypatch.setattr(rj.urllib.request, "urlopen", _paged([page_one, page_two]))
    assert judge._review_exists_at_sha() is True, "page 2 was never fetched"


def test_ignores_a_review_forged_by_another_author(judge, monkeypatch):
    """The marker is public text; only the author identity is unforgeable.

    Treating a non-judge review as the judge's own suppresses the real verdict — a
    silent failure, since the run still reports success.
    """
    forged = [_review(rj.REVIEW_MARKER, author="some-collaborator")]
    monkeypatch.setattr(rj.urllib.request, "urlopen", _paged([forged]))
    assert judge._review_exists_at_sha() is False, "a non-judge review was accepted"


def test_still_matches_a_genuine_judge_review(judge, monkeypatch):
    """The author filter must not break the idempotency it is guarding."""
    page = [_review(rj.REVIEW_MARKER + "\n## PR Assessment Review")]
    monkeypatch.setattr(rj.urllib.request, "urlopen", _paged([page]))
    assert judge._review_exists_at_sha() is True


def test_ignores_a_review_on_a_different_sha(judge, monkeypatch):
    page = [_review(rj.REVIEW_MARKER, commit_id="deadbeef")]
    monkeypatch.setattr(rj.urllib.request, "urlopen", _paged([page]))
    assert judge._review_exists_at_sha() is False


def test_api_error_fails_open(judge, monkeypatch):
    """Unchanged behaviour: a lookup failure must not block the verdict from posting."""

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(rj.urllib.request, "urlopen", boom)
    assert judge._review_exists_at_sha() is False


def test_malformed_response_fails_open(judge, monkeypatch):
    """A non-list payload must not be iterated as if it were reviews."""

    def not_a_list(req, timeout=None):
        return _FakeResponse(json.dumps({"message": "Not Found"}).encode())

    monkeypatch.setattr(rj.urllib.request, "urlopen", not_a_list)
    assert judge._review_exists_at_sha() is False


def test_page_cap_is_bounded(judge, monkeypatch):
    """A server that never returns a short page must not spin the loop forever."""
    calls = {"n": 0}

    def always_full(req, timeout=None):
        calls["n"] += 1
        # Always a full page, never the SHA — the only exit is the page cap.
        page = [_review(rj.REVIEW_MARKER, commit_id="other")] * rj._REVIEWS_PER_PAGE
        return _FakeResponse(json.dumps(page).encode())

    monkeypatch.setattr(rj.urllib.request, "urlopen", always_full)
    assert judge._review_exists_at_sha() is False
    assert calls["n"] == rj._MAX_REVIEW_PAGES, (
        f"expected the walk to stop at the {rj._MAX_REVIEW_PAGES}-page cap, "
        f"got {calls['n']} requests"
    )


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


PLUGIN_JUDGE_COPY = REPO_ROOT / "plugin" / "manolii-framework" / "scripts" / "run-judge.py"


@pytest.mark.skipif(
    not PLUGIN_JUDGE_COPY.exists(),
    reason="plugin/ is pack-internal (copier.yml _exclude) — absent in rendered instances",
)
def test_plugin_copy_is_in_sync():
    """The pack ships two copies; a fix applied to one only is a silent regression.

    Guarded on the plugin copy's existence. `plugin/**` is a build artifact
    excluded from rendered consumers by copier.yml, but this whole test module
    IS shipped — so without the guard every rendered project inherited a test
    that failed on first run with FileNotFoundError. The parity check still runs
    (unskipped) in the pack repo, which is the only place both copies exist and
    the only place drift is possible.
    """
    a = (REPO_ROOT / "scripts" / "run-judge.py").read_text()
    b = PLUGIN_JUDGE_COPY.read_text()
    assert a == b, "scripts/run-judge.py and the plugin copy have drifted"
