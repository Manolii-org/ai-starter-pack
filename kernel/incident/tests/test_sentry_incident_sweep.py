from __future__ import annotations

import importlib.util
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sweep", ROOT / "bin" / "sentry_incident_sweep.py")
sweep_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep_mod)  # type: ignore[union-attr]

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
MANIFEST = {
    "entity": "example",
    "organization": {"slug": "example-org", "id": "1000000000000001"},
    "routes": [
        {"project_id": "11", "environment": "production", "repo": "Example-org/api", "runtime_id": "production:Example-org/api"},
        {"project_id": "11", "environment": "scrape", "repo": "Example-org/scrape", "runtime_id": "production:Example-org/scrape"},
    ],
}
END = '<https://sentry.io/>; rel="next"; results="false"; cursor="0:0:0"'


def issue(i="42", project="11", level="error", short="API-1G"):
    return {"id": i, "project": {"id": project}, "shortId": short, "level": level,
            "title": "secret=private user@example.com", "metadata": {"value": "private"}}


def fake(pages: dict[str, list], events: dict[str, object] | None = None, calls: list | None = None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "/events/latest/" in parsed.path:
            issue_id = parsed.path.split("/issues/")[1].split("/")[0]
            value = (events or {}).get(issue_id, {})
            if isinstance(value, Exception):
                raise value
            return value, {}
        return pages.get(query["environment"][0], []), {"Link": END}
    return fetch


def event(type_="TypeError", value="private secret=x", in_app=True, release="api@0123456789abcdef0123456789abcdef01234567"):
    return {"release": {"version": release}, "entries": [{"type": "exception", "data": {"values": [
        {"type": type_, "value": value, "stacktrace": {"frames": [{"inApp": False}, {"inApp": in_app}]}}]}}]}


def test_classification_corpus_is_shared_parity_contract():
    rows = [json.loads(line) for line in (ROOT / "classification-cases.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) >= 10
    for row in rows:
        assert sweep_mod.classify(row["type"], row["message"], row["in_app"], row["sha"]) == row["expect"], row


def test_emits_schema_shaped_candidates_routed_by_project_and_environment():
    result = sweep_mod.sweep(MANIFEST, fake({"production": [issue()], "scrape": [issue("7", short="API-2")]},
                                            {"42": event(), "7": event("RateLimitError", "429")}), now=NOW)
    by_repo = {c["repo"]: c for c in result["candidates"]}
    api = by_repo["Example-org/api"]
    assert api["classification"] == "code_defect"
    assert api["release_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert api["title"] == "Sentry API-1G: TypeError"
    assert api["environment"] == "production"
    assert api["provider_account_id"] == "1000000000000001"
    assert api["delivery_id"] == "sentry:issue:42:production:2026-11-12"
    assert api["telemetry_links"] == ["https://example-org.sentry.io/issues/42/"]
    assert by_repo["Example-org/scrape"]["classification"] == "quota"
    assert by_repo["Example-org/scrape"]["runtime_id"] == "production:Example-org/scrape"
    # Messages, titles and metadata never leave the sweep.
    assert "private" not in json.dumps(result) and "example.com" not in json.dumps(result)


def test_fingerprint_is_stable_per_group_across_days_and_releases():
    a = sweep_mod.sweep(MANIFEST, fake({"production": [issue()]}, {"42": event()}), now=NOW)["candidates"][0]
    b = sweep_mod.sweep(MANIFEST, fake({"production": [issue()]}, {"42": event(release="api@" + "b" * 40)}),
                        now=datetime(2026, 11, 13, tzinfo=timezone.utc))["candidates"][0]
    assert a["fingerprint"] == b["fingerprint"]
    assert a["delivery_id"] != b["delivery_id"]


def test_noise_and_fly_ulid_releases_are_never_code_defects():
    result = sweep_mod.sweep(MANIFEST, fake({"production": [issue("1"), issue("2"), issue("3")]}, {
        "1": event(in_app=False), "2": event(release="01M78575ENSGQ3PSKCZPWSC47P"), "3": {}}), now=NOW)
    assert {c["classification"] for c in result["candidates"]} == {"unknown"}


def test_secret_shaped_exception_types_are_dropped():
    for secret in ("sk_test_PRIVATE", "ghp_abc", "a1b2c3d4e5f6a7b8c9d0e1"):
        assert sweep_mod.safe_exception_type(secret) is None
    for name in ("ContentPolicyViolationError", "Http2ServerError", "litellm.RateLimitError"):
        assert sweep_mod.safe_exception_type(name) == name


def test_enrichment_budget_never_drops_a_group():
    rows = [issue(str(i)) for i in range(1, 6)]
    result = sweep_mod.sweep(MANIFEST, fake({"production": rows}, {str(i): event() for i in range(1, 6)}),
                             now=NOW, max_enrichments=3)
    assert len(result["candidates"]) == 5
    assert result["stats"]["deferred"] == 2
    assert sum(c["classification"] == "unknown" for c in result["candidates"]) == 2


def test_slow_enrichment_stops_at_its_budget_and_still_emits_everything():
    ticks = iter(range(0, 1000, 50))
    rows = [issue(str(i)) for i in range(1, 6)]
    result = sweep_mod.sweep(MANIFEST, fake({"production": rows}, {str(i): event() for i in range(1, 6)}),
                             now=NOW, enrich_seconds=120, clock=lambda: next(ticks))
    assert len(result["candidates"]) == 5
    assert result["stats"]["deferred"] >= 1


def test_unreadable_event_is_emitted_unknown_not_dropped():
    result = sweep_mod.sweep(MANIFEST, fake({"production": [issue()]}, {"42": sweep_mod.SweepError("HTTP 500")}), now=NOW)
    assert result["candidates"][0]["classification"] == "unknown"
    assert result["stats"]["unreadable"] == 1


def test_fails_closed_on_unrouted_project_bad_pages_and_foreign_origin():
    with pytest.raises(sweep_mod.SweepError, match="explicit route"):
        sweep_mod.sweep(MANIFEST, fake({"production": [issue(project="99")]}), now=NOW)
    def missing_link(url):
        return [issue(str(i)) for i in range(100)], {}
    with pytest.raises(sweep_mod.SweepError, match="incomplete"):
        sweep_mod.sweep(MANIFEST, missing_link, now=NOW)
    with pytest.raises(sweep_mod.SweepError, match="another origin"):
        sweep_mod.http_fetch("t")("https://evil.example/api/0/x")


def test_queries_each_environment_with_only_its_projects():
    calls: list[str] = []
    sweep_mod.sweep(MANIFEST, fake({}, calls=calls), now=NOW)
    envs = {}
    for url in calls:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        envs[q["environment"][0]] = q["project"]
        assert url.startswith("https://sentry.io/api/0/organizations/example-org/issues/?")
        assert "lastSeen:>=2026-11-12T11:00:00Z" in q["query"][0]
    assert envs == {"production": ["11"], "scrape": ["11"]}


def test_manifest_rejects_duplicate_routes(tmp_path):
    bad = dict(MANIFEST, routes=MANIFEST["routes"] + [MANIFEST["routes"][0]])
    path = tmp_path / "r.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(sweep_mod.SweepError, match="duplicate"):
        sweep_mod.load_routes(path)


def test_example_manifest_loads():
    assert sweep_mod.load_routes(ROOT / "manifest" / "examples" / "example.json")["entity"] == "example"


def test_budget_defers_the_newest_groups_first():
    rows = [dict(issue(str(i)), lastSeen=f"2026-11-12T11:{50 - i:02d}:00Z") for i in range(1, 6)]
    result = sweep_mod.sweep(MANIFEST, fake({"production": rows}, {str(i): event() for i in range(1, 6)}),
                             now=NOW, max_enrichments=3)
    enriched = {c["delivery_id"].split(":")[2] for c in result["candidates"] if c["classification"] != "unknown"}
    assert enriched == {"3", "4", "5"}  # the three oldest; 1 and 2 (newest) are emitted unknown


def test_redirects_are_refused_so_the_token_never_follows_them():
    handler = sweep_mod._NoRedirect()
    with pytest.raises(sweep_mod.SweepError, match="redirect refused"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/")


def test_cursor_is_read_from_the_link_url_when_the_attribute_is_absent():
    link = '<https://sentry.io/api/0/organizations/o/issues/?cursor=100:0:1>; rel="next"; results="true"'
    assert sweep_mod._next_cursor(link, 100) == "100:0:1"
    bad = '<https://sentry.io/api/0/organizations/o/issues/?cursor=../../x>; rel="next"; results="true"'
    with pytest.raises(sweep_mod.SweepError):
        sweep_mod._next_cursor(bad, 100)
    with pytest.raises(sweep_mod.SweepError):
        sweep_mod._next_cursor('<https://sentry.io/>; rel="next"; results="true"', 100)
