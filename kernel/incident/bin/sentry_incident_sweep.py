#!/usr/bin/env python3
"""Sentry incident sweep: a portable, entity-local producer of sanitized
production-incident candidates.

It reads one entity's Sentry organization and, for each explicitly routed
(project id, environment), lists unresolved error/fatal issue groups seen in
the lookback window. For each group it reads the latest event once and
classifies it deterministically with ``signatures.json``. It emits one
candidate envelope per group, in the shape documented in ``candidate-shape.md``.

It never writes to Sentry, GitHub or any datastore. The consumer (for
example an orchestrator's production-incident intake) owns admission, attestation,
per-entity switches, caps, deduplication and execution.

Privacy: the exception message is matched in memory only. Candidates carry
ids, the Sentry short id, an identifier-shaped exception type, a SHA-only
release, a classification and a provider link.

Usage:
  sentry_incident_sweep.py --routes routes.json [--lookback-minutes N] > candidates.jsonl
  (token from SENTRY_INCIDENT_SWEEP_TOKEN; never passed on the command line)

Stdlib only, Python 3.10+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

API = "https://sentry.io/api/0"  # fixed origin: the bearer token is never sent elsewhere
PAGE_SIZE = 100
MAX_PAGES = 5
DEFAULT_MAX_ENRICHMENTS = 60
HERE = Path(__file__).resolve().parent.parent
SIGNATURES = json.loads((HERE / "signatures.json").read_text(encoding="utf-8"))
COMPILED = [(row["classification"], re.compile(row["pattern"], re.IGNORECASE)) for row in SIGNATURES["signatures"]]
IDENT = re.compile(r"^[A-Za-z_][\w.$]{0,80}$")
SECRET_SHAPED = re.compile(
    r"(?:^|[._$])(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abprs]|AKIA|sbp|dp)[_.-]|[0-9a-f]{16,}|(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{20,}",
    re.IGNORECASE,
)
SHA_TAIL = re.compile(r"(?:^|@|[-+])([0-9a-f]{7,64})$", re.IGNORECASE)
HEX = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
SHORT_ID = re.compile(r"^[A-Z0-9-]{1,100}$")
CURSOR = re.compile(r"^[0-9:-]{1,100}$")

Fetch = Callable[[str], tuple[object, dict]]


class SweepError(RuntimeError):
    """Coverage could not be completed; never report a partial sweep as whole."""


def classify(exception_type: str | None, message: str | None, in_app: bool | None, sha: str | None) -> str:
    blob = f"{exception_type or ''} {message or ''}"
    for classification, pattern in COMPILED:
        if pattern.search(blob):
            return classification
    return "code_defect" if in_app is True and sha is not None else "unknown"


def safe_exception_type(value: object) -> str | None:
    if not isinstance(value, str) or not IDENT.match(value) or SECRET_SHAPED.search(value):
        return None
    return value


def release_sha(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 100:
        return None
    match = SHA_TAIL.search(value)
    if match:
        return match.group(1).lower()
    return value.lower() if HEX.match(value) else None


def event_facts(event: object) -> dict:
    data = event if isinstance(event, dict) else {}
    release = data.get("release")
    if isinstance(release, dict):
        release = release.get("version")
    if not isinstance(release, str):
        release = next((t.get("value") for t in data.get("tags") or [] if isinstance(t, dict) and t.get("key") == "release"), None)
    exception = next((e for e in data.get("entries") or [] if isinstance(e, dict) and e.get("type") == "exception"), None)
    values = ((exception or {}).get("data") or {}).get("values") or []
    values = values if isinstance(values, list) else []
    last = values[-1] if values and isinstance(values[-1], dict) else {}
    in_app = None if exception is None else any(
        isinstance(frame, dict) and frame.get("inApp") is True
        for value in values if isinstance(value, dict)
        for frame in ((value.get("stacktrace") or {}).get("frames") or [])
    )
    message = last.get("value") if isinstance(last.get("value"), str) else data.get("message")
    return {
        "exception_type": safe_exception_type(last.get("type")),
        "message": message[:500] if isinstance(message, str) else None,
        "in_app": in_app,
        "release": release if isinstance(release, str) else None,
    }


def http_fetch(token: str) -> Fetch:
    def fetch(url: str) -> tuple[object, dict]:
        if not url.startswith(API + "/"):
            raise SweepError("refusing to send the Sentry token to another origin")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https origin
                return json.loads(response.read() or b"null"), dict(response.headers)
        except urllib.error.HTTPError as exc:
            raise SweepError(f"Sentry API HTTP {exc.code}") from None
    return fetch


def _next_cursor(link: str | None, page_length: int) -> str | None:
    part = next((p for p in (link or "").split(",") if 'rel="next"' in p), None)
    if part is None:
        if page_length == PAGE_SIZE:
            raise SweepError("Sentry pagination is incomplete")
        return None
    if 'results="false"' in part:
        return None
    match = re.search(r'cursor="([^"]+)"', part)
    if 'results="true"' not in part or not match or not CURSOR.match(match.group(1)):
        raise SweepError("Sentry pagination state is invalid")
    return match.group(1)


def load_routes(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for key in ("entity", "organization", "routes"):
        if key not in manifest:
            raise SweepError(f"routes manifest missing {key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", manifest["organization"].get("slug", "")) \
            or not re.fullmatch(r"[0-9]{1,32}", str(manifest["organization"].get("id", ""))):
        raise SweepError("routes manifest organization is invalid")
    seen = set()
    for route in manifest["routes"]:
        key = (route["project_id"], route["environment"])
        if key in seen:
            raise SweepError("duplicate (project_id, environment) route")
        seen.add(key)
    return manifest


def sweep(manifest: dict, fetch: Fetch, *, now: datetime | None = None, lookback_minutes: int | None = None,
          max_enrichments: int | None = None, deadline_seconds: float = 240.0,
          clock: Callable[[], float] = time.monotonic) -> dict:
    """Return {"candidates": [...], "stats": {...}}. Raises SweepError on incomplete coverage."""
    started = clock()
    now = now or datetime.now(timezone.utc)
    lookback = lookback_minutes or int(manifest.get("lookback_minutes", 60))
    if not 15 <= lookback <= 10080:
        raise SweepError("lookback must be between 15 minutes and 7 days")
    budget = max_enrichments or int(manifest.get("max_enrichments", DEFAULT_MAX_ENRICHMENTS))
    org = manifest["organization"]
    since = (now - timedelta(minutes=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_env: dict[str, dict[str, dict]] = {}
    for route in manifest["routes"]:
        by_env.setdefault(route["environment"], {})[route["project_id"]] = route
    groups: dict[str, tuple[dict, dict, str]] = {}
    pages = 0
    for environment, projects in sorted(by_env.items()):
        params = [("query", f"is:unresolved level:[error,fatal] lastSeen:>={since}"),
                  ("environment", environment), ("limit", str(PAGE_SIZE))]
        params += [("project", pid) for pid in sorted(projects)]
        cursors: set[str] = set()
        env_pages = 0
        cursor = None
        while True:
            if clock() - started > deadline_seconds:
                raise SweepError("sweep deadline reached; coverage is incomplete")
            query = params + ([("cursor", cursor)] if cursor else [])
            rows, headers = fetch(f"{API}/organizations/{org['slug']}/issues/?{urllib.parse.urlencode(query)}")
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise SweepError("Sentry issue page is invalid")
            for row in rows:
                project_id = str(((row or {}).get("project") or {}).get("id", ""))
                route = projects.get(project_id)
                if route is None:
                    raise SweepError("Sentry issue project has no explicit route")
                issue_id = str(row.get("id", ""))
                if not re.fullmatch(r"\d{1,32}", issue_id):
                    raise SweepError("Sentry issue id is invalid")
                groups[f"{issue_id}:{environment}"] = (row, route, environment)
            pages += 1
            env_pages += 1
            cursor = _next_cursor(headers.get("Link") or headers.get("link"), len(rows))
            if cursor is None:
                break
            if env_pages >= MAX_PAGES or cursor in cursors:
                raise SweepError("Sentry pagination coverage is incomplete")
            cursors.add(cursor)

    candidates, deferred, enriched, unreadable = [], 0, 0, 0
    # Oldest lastSeen first: groups nearest the lookback edge are enriched now,
    # so a deferred group is among the newest and is re-observed next sweep.
    ordered = sorted(groups.values(), key=lambda item: str(item[0].get("lastSeen") or ""))
    for index, (row, route, environment) in enumerate(ordered):
        if index >= budget or clock() - started > deadline_seconds:
            deferred += 1  # re-observed by the next sweep while it recurs
            continue
        issue_id = str(row["id"])
        try:
            event, _ = fetch(f"{API}/organizations/{org['slug']}/issues/{issue_id}/events/latest/?"
                             + urllib.parse.urlencode({"environment": environment}))
            facts = event_facts(event)
            enriched += 1
        except SweepError:
            facts = {"exception_type": None, "message": None, "in_app": None, "release": None}
            unreadable += 1
        sha = release_sha(facts["release"])
        short_id = row.get("shortId") if isinstance(row.get("shortId"), str) and SHORT_ID.match(row["shortId"]) else issue_id
        exception_type = facts["exception_type"]
        candidates.append({
            "entity": manifest["entity"],
            "repo": route["repo"],
            "runtime_id": route["runtime_id"],
            "provider": "sentry",
            "provider_account_id": str(org["id"]),
            "provider_project_id": route["project_id"],
            "delivery_id": f"sentry:issue:{issue_id}:{environment}:{now.date().isoformat()}",
            "release_sha": sha,
            "fingerprint": hashlib.sha256(f"sentry-group:{issue_id}".encode()).hexdigest(),
            "title": f"Sentry {short_id}" + (f": {exception_type}" if exception_type else ""),
            "classification": classify(exception_type, facts["message"], facts["in_app"], sha),
            "severity": "fatal" if row.get("level") == "fatal" else "error",
            "environment": "production",
            "telemetry_links": [f"https://{org['slug']}.sentry.io/issues/{issue_id}/"],
        })
    return {"candidates": candidates, "stats": {
        "since": since, "pages": pages, "groups": len(groups), "enriched": enriched,
        "unreadable": unreadable, "deferred": deferred,
    }}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--lookback-minutes", type=int)
    parser.add_argument("--stats-out", type=Path, help="write sweep statistics JSON here (stdout carries candidates)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    token = os.environ.get("SENTRY_INCIDENT_SWEEP_TOKEN", "")
    if not token:
        print("SENTRY_INCIDENT_SWEEP_TOKEN is not set", file=sys.stderr)
        return 2
    try:
        result = sweep(load_routes(args.routes), http_fetch(token), lookback_minutes=args.lookback_minutes)
    except SweepError as exc:
        print(f"sweep failed: {exc}", file=sys.stderr)
        return 1
    for candidate in result["candidates"]:
        print(json.dumps(candidate, sort_keys=True))
    if args.stats_out:
        args.stats_out.write_text(json.dumps(result["stats"], sort_keys=True), encoding="utf-8")
    print(json.dumps(result["stats"], sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
