#!/usr/bin/env python3
"""gha-cost-sample.py — GitHub Actions cost attribution sampler (stdlib only).

Reimplements the method in the ci-cost task spec, section 5:
  * list workflow runs per workflow per UTC day (stays under the ~1000-result cap)
  * fetch each run's jobs (filter=latest)
  * classify jobs by runner labels -> hosted-linux / slim / arm / macos / self-hosted
  * billed minutes per job = max(1, ceil((completed_at - started_at) / 60))
  * dollars = billed_min * rate; self-hosted = $0 today
  * skipped jobs and jobs missing timestamps do not bill
  * push census: dedupe event=='push' runs by head_sha per day, grouped by head_branch
    (never by the pull_requests field — it is empty on most push runs)

Raw API responses are cached under --out-dir so re-runs are idempotent and
offline-inspectable. Auth: $GH_TOKEN / $GITHUB_TOKEN / $GH_AUTOMATION_TOKEN.

Rates (docs.github.com billing reference, verified 2026-09): linux 0.006,
slim 0.002, arm64 0.005, macos-3core 0.062 per minute.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
RATES = {
    "linux": 0.006,
    "windows": 0.012,  # 2x Linux per GitHub hosted multiplier
    "slim": 0.002,
    "arm": 0.005,
    "macos": 0.062,
    "self-hosted": 0.0,
    "unknown": 0.006,  # conservative: assume hosted price
}
USER_AGENT = "gha-cost-sample/1.0"


LOG_FILE = None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a") as f:
            f.write(msg + "\n")


class Client:
    def __init__(self, token: str, out_dir: str, max_retries: int = 4):
        self.token = token
        self.out_dir = out_dir
        self.max_retries = max_retries
        self.calls = 0
        os.makedirs(out_dir, exist_ok=True)

    def _cache_path(self, key: str) -> str:
        # reversible encoding — colliding repos like a/b_c vs a_b/c must not
        # share cache entries
        safe = urllib.parse.quote(key, safe="")
        return os.path.join(self.out_dir, safe + ".json")

    def get(self, url: str, cache_key: str | None = None, use_cache: bool = True):
        if cache_key and use_cache:
            p = self._cache_path(cache_key)
            if os.path.exists(p):
                with open(p) as f:
                    return json.load(f)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        attempt = 0
        while True:
            self.calls += 1
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                    rem = resp.headers.get("X-RateLimit-Remaining")
                    if rem is not None and int(rem) < 100:
                        reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
                        wait = max(0, reset - int(time.time())) + 5
                        log(f"  rate-limit remaining={rem}; sleeping {wait}s until reset")
                        time.sleep(min(wait, 600))
                    break
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:400]
                attempt += 1
                if e.code in (403, 429) and attempt <= self.max_retries:
                    ra = e.headers.get("Retry-After")
                    wait = int(ra) if ra else min(60, 5 * 2 ** attempt) + random.uniform(0, 3)
                    log(f"  HTTP {e.code} on {url} (attempt {attempt}); sleeping {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {e.code} on {url}: {body}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                attempt += 1
                if attempt <= self.max_retries:
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
                raise RuntimeError(f"request failed {url}: {e}")
        if cache_key:
            with open(self._cache_path(cache_key), "w") as f:
                json.dump(data, f)
        return data


def daterange(since: str, until: str):
    d0 = dt.date.fromisoformat(since)
    d1 = dt.date.fromisoformat(until)
    while d0 <= d1:
        yield d0.isoformat()
        d0 += dt.timedelta(days=1)


def list_workflows(client: Client, repo: str):
    # never cached — a workflow added between runs must enter the census; the
    # inventory is one page call and not worth a stale-read
    out = []
    page = 1
    while True:
        d = client.get(f"{API}/repos/{repo}/actions/workflows?per_page=100&page={page}",
                       use_cache=False)
        out.extend(d.get("workflows", []))
        if len(out) >= d.get("total_count", 0) or not d.get("workflows"):
            break
        page += 1
    return out


def list_runs_for_day(client: Client, repo: str, wf_id: int, day: str, event: str | None = None):
    """All runs of workflow wf_id created on `day` (UTC), plus a truncated flag.

    Returns (runs, truncated): the GitHub runs listing caps at 1000 results
    per query — a day over that returns a truncated list with truncated=True
    so callers can mark the aggregation incomplete instead of presenting
    it as a full census."""
    runs = []
    page = 1
    ev = f"&event={event}" if event else ""
    while True:
        url = (f"{API}/repos/{repo}/actions/workflows/{wf_id}/runs"
               f"?created={day}..{day}&per_page=100&page={page}{ev}")
        # never cached — a rerun mutates the page for the run's original
        # created day (run_attempt bumps) no matter how old it is, and the
        # jobs-fetch keys on run_attempt, so a stale page drops retried
        # minutes; queued runs completing are the same class
        d = client.get(url, cache_key=f"runs-{repo}-{wf_id}-{day}-{page}{ev}",
                       use_cache=False)
        batch = d.get("workflow_runs", [])
        runs.extend(batch)
        total = d.get("total_count", 0)
        if len(batch) < 100:
            return runs, False
        if len(runs) >= 1000:
            log(f"  WARNING: {repo} wf {wf_id} {day}: hit 1000-result cap at total_count={total}; day needs finer split")
            return runs, True
        page += 1


def list_push_events(client: Client, repo: str, since: str):
    """PushEvents for `repo` back to `since` (UTC day), (events, truncated).

    The runs endpoint cannot see a push that triggers zero workflows — the
    Events API is the only feed that enumerates the pushes themselves, and
    it is independent of the --workflows filter. GitHub caps the public feed
    at ~300 events; hitting it marks the census incomplete rather than
    silently undercounting."""
    events = []
    oldest_seen = None
    full_pages = 0
    page = 1
    while page <= 10:
        # use_cache=False: the events feed is a sliding window — a cached page
        # from a previous sampler run would hide newer pushes and undercount.
        try:
            batch = client.get(f"{API}/repos/{repo}/events?per_page=100&page={page}",
                               cache_key=f"events-{repo}-{page}", use_cache=False)
        except RuntimeError as e:
            # GitHub's events feed 422s past its ~300-event pagination cap on
            # busy repos — degrade to a truncated push census rather than
            # crashing the whole repo enumeration.
            if "HTTP 422" in str(e):
                log(f"  WARNING: {repo}: push-events feed refused page {page} (pagination cap); pushes_by_day undercounts")
                return events, True
            raise
        events.extend(e for e in batch if e.get("type") == "PushEvent")
        if batch:
            oldest_seen = (batch[-1].get("created_at") or "")[:10]
        if oldest_seen is not None and oldest_seen < since:
            return events, False  # feed reached past the window — census covers `since`
        if len(batch) < 100:
            # Retained feed exhausted. <3 full pages means repo history is
            # shorter than the ~300-event retention cap, so nothing was cut;
            # reaching the cap without covering `since` means older pushes
            # were dropped — flag the census incomplete instead of a silent
            # undercount.
            # the feed only retains ~90 days of events; if `since` predates
            # retention the census can never cover the window even though the
            # feed looks naturally short (a quiet repo is indistinguishable
            # from expired history) — flag it rather than undercount
            retention_floor = (dt.datetime.now(dt.timezone.utc).date() -
                               dt.timedelta(days=89)).isoformat()
            truncated = full_pages >= 3 or since < retention_floor
            if truncated:
                log(f"  WARNING: {repo}: event feed ended at ~300 events before {since}; pushes_by_day undercounts")
            return events, truncated
        full_pages += 1
        page += 1
    log(f"  WARNING: {repo}: push census hit the ~300-event feed cap; pushes_by_day undercounts")
    return events, True


def list_jobs(client: Client, repo: str, run_id: int, attempt: int = 1, mutable: bool = False):
    jobs = []
    page = 1
    while True:
        # filter=all: rerun attempts each consume billed runner time; latest
        # alone would drop every earlier attempt's minutes from the numerator.
        # attempt in the cache key: a retried run gets a fresh fetch instead of
        # the previous attempt's stale page; mutable runs bypass the cache.
        d = client.get(f"{API}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=all&page={page}",
                       cache_key=f"jobs-{repo}-{run_id}-a{attempt}-{page}",
                       use_cache=not mutable)
        batch = d.get("jobs", [])
        jobs.extend(batch)
        if len(batch) < 100 or len(jobs) >= d.get("total_count", 0):
            break
        page += 1
    return jobs


def classify(labels: list[str]) -> str:
    labs = [l.lower() for l in labels]
    if any("self-hosted" in l or l == "fly" for l in labs):
        return "self-hosted"
    if any("macos" in l for l in labs):
        return "macos"
    if any("arm" in l for l in labs):
        return "arm"
    if any("slim" in l for l in labs):
        return "slim"
    if any("windows" in l for l in labs):
        return "windows"
    if any(l.startswith("ubuntu") or "linux" in l for l in labs):
        return "linux"
    return "unknown"


def job_billed_min(job: dict) -> float:
    if job.get("conclusion") == "skipped" or job.get("status") == "skipped":
        return 0.0
    s, c = job.get("started_at"), job.get("completed_at")
    if not s or not c:
        return 0.0
    t0 = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    t1 = dt.datetime.fromisoformat(c.replace("Z", "+00:00"))
    return max(1, math.ceil((t1 - t0).total_seconds() / 60))


def main() -> int:
    ap = argparse.ArgumentParser(description="GHA cost attribution sampler (stdlib only)")
    ap.add_argument("--repo", required=True, action="append", help="owner/name (repeatable)")
    ap.add_argument("--since", required=True, help="first UTC day YYYY-MM-DD (inclusive)")
    ap.add_argument("--until", required=True, help="last UTC day YYYY-MM-DD (inclusive)")
    ap.add_argument("--workflows", default="", help="comma list of workflow filenames/names to include (default: all)")
    ap.add_argument("--sample", type=int, default=0, help="if >0, fetch jobs for N runs per workflow picked evenly across the ID-sorted list; 0 = exact (all runs)")
    ap.add_argument("--jobs-for-top", type=int, default=0, help="exact job fetch only for top N workflows by run count; others get --sample")
    ap.add_argument("--jobs-workflows", default="", help="comma list of workflow filenames that get exact job fetch (all others get --sample)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--json-out", default="", help="write full summary JSON here")
    ap.add_argument("--log-file", default="", help="append progress log lines to this file")
    args = ap.parse_args()
    if args.sample < 0 or args.jobs_for_top < 0:
        ap.error("--sample and --jobs-for-top must be >= 0")
    if args.until < args.since:
        ap.error(f"--until ({args.until}) is before --since ({args.since})")
    global LOG_FILE
    LOG_FILE = args.log_file or None

    token_file = os.environ.get("GH_TOKEN_FILE", "")
    token = ""
    if token_file and os.path.exists(token_file):
        with open(token_file) as tf:
            token = tf.read().strip()
    token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_AUTOMATION_TOKEN") or ""
    if not token:
        log("WARNING: no GH_TOKEN/GITHUB_TOKEN/GH_AUTOMATION_TOKEN — anonymous rate limits apply")
    client = Client(token, args.out_dir)
    wf_filter = {w.strip() for w in args.workflows.split(",") if w.strip()}
    summary = {"repos": {}, "api_calls": 0}

    for repo in args.repo:
        log(f"== {repo} {args.since}..{args.until}")
        workflows = list_workflows(client, repo)
        if wf_filter:
            workflows = [w for w in workflows if w["path"].split("/")[-1] in wf_filter or w["name"] in wf_filter]
        # pass 1: run listings (metadata only)
        wf_runs: dict[int, list[dict]] = {}
        capped_wfs: set[int] = set()
        for wf in workflows:
            runs = []
            truncated = False
            for day in daterange(args.since, args.until):
                day_runs, day_truncated = list_runs_for_day(client, repo, wf["id"], day)
                runs.extend(day_runs)
                truncated = truncated or day_truncated
            if runs:
                wf_runs[wf["id"]] = runs
                if truncated:
                    capped_wfs.add(wf["id"])
            log(f"   {wf['name']:45s} {wf['path'].split('/')[-1]:40s} {len(runs):5d} runs")
        # pass 2: jobs
        jw_filter = {w.strip() for w in args.jobs_workflows.split(",") if w.strip()}
        order = sorted(wf_runs.items(), key=lambda kv: -len(kv[1]))
        if jw_filter:
            exact_ids = {wf["id"] for wf in workflows if wf["path"].split("/")[-1] in jw_filter or wf["name"] in jw_filter}
        else:
            exact_ids = {wid for wid, _ in order[: args.jobs_for_top]}
            # jobs_for_top=0 -> empty exact set, so --sample applies to every
            # workflow; with no --sample all runs are fetched as before.
        repo_sum = {"workflows": {}, "pushes_by_day": {}, "totals": {}}
        push_shas: dict[str, set] = {}
        for wf in workflows:
            wid = wf["id"]
            runs = wf_runs.get(wid, [])
            if not runs:
                continue
            fetch = runs
            sampled = False
            if wid not in exact_ids and args.sample > 0 and len(runs) > args.sample:
                ordered = sorted(runs, key=lambda r: r["id"])
                step = len(ordered) / args.sample
                fetch = [ordered[int(i * step)] for i in range(args.sample)]
                sampled = True
            agg = {}
            job_names = {}
            n_billed = 0
            for r in fetch:
                if r.get("status") != "completed" or r.get("conclusion") == "skipped":
                    continue
                n_billed += 1
                run_created = r.get("created_at", "")
                mutable = (r.get("status") != "completed" or
                           (r.get("created_at") or "")[:10] >=
                           (dt.datetime.now(dt.timezone.utc).date() -
                            dt.timedelta(days=2)).isoformat())
                for job in list_jobs(client, repo, r["id"],
                                     attempt=int(r.get("run_attempt") or 1),
                                     mutable=mutable):
                    cls = classify(job.get("labels") or [])
                    bm = job_billed_min(job)
                    if bm == 0:
                        continue
                    agg[cls] = agg.get(cls, 0) + bm
                    jn = job.get("name", "?")
                    j = job_names.setdefault(jn, {"runs": 0, "billed_min": 0.0, "cls": cls,
                                                  "durations": [], "waits": []})
                    j["runs"] += 1
                    j["billed_min"] += bm
                    s, c = job.get("started_at"), job.get("completed_at")
                    if s and c:
                        j["durations"].append(round((dt.datetime.fromisoformat(c.replace("Z", "+00:00")) -
                                                     dt.datetime.fromisoformat(s.replace("Z", "+00:00"))).total_seconds()))
                    if s and run_created:
                        j["waits"].append(round((dt.datetime.fromisoformat(s.replace("Z", "+00:00")) -
                                                 dt.datetime.fromisoformat(run_created.replace("Z", "+00:00"))).total_seconds()))
            scale = (len(runs) / len(fetch)) if fetch else 1.0
            cost = sum(agg.get(c, 0) * RATES[c] for c in agg)
            repo_sum["workflows"][wf["path"].split("/")[-1]] = {
                "name": wf["name"], "runs_total": len(runs), "runs_costed": len(fetch),
                "completed_runs": n_billed, "sampled": sampled, "scale": round(scale, 3),
                "incomplete": wid in capped_wfs,
                "billed_min": {k: round(v, 1) for k, v in sorted(agg.items())},
                "billed_min_scaled": {k: round(v * scale, 1) for k, v in sorted(agg.items())},
                "est_cost_usd": round(cost * scale, 2),
                "jobs": {k: {"runs": v["runs"], "billed_min": round(v["billed_min"], 1), "cls": v["cls"],
                             "durations": sorted(v["durations"]), "waits": sorted(v["waits"])}
                          for k, v in sorted(job_names.items(), key=lambda kv: -kv[1]["billed_min"])},
            }
        push_events, push_census_truncated = list_push_events(client, repo, args.since)
        for e in push_events:
            day = (e.get("created_at") or "")[:10]
            if day < args.since or day > args.until:
                continue
            # dedup by event id: every PushEvent is a push — a reset-and-repush
            # to a previously-seen head (or a null head) still counts
            push_shas.setdefault(day, set()).add(e.get("id"))
        repo_sum["pushes_by_day"] = {d: len(s) for d, s in sorted(push_shas.items())}
        if push_census_truncated:
            repo_sum["push_census_incomplete"] = True
        tot_cost = sum(w["est_cost_usd"] for w in repo_sum["workflows"].values())
        tot_min = {}
        for w in repo_sum["workflows"].values():
            for k, v in w["billed_min_scaled"].items():
                tot_min[k] = tot_min.get(k, 0) + v
        repo_sum["totals"] = {"est_cost_usd": round(tot_cost, 2),
                              "incomplete_workflows": sorted(capped_wfs),
                              "billed_min_scaled": {k: round(v, 1) for k, v in sorted(tot_min.items())}}
        summary["repos"][repo] = repo_sum
    summary["api_calls"] = client.calls
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(summary, f, indent=2)
    # print compact table
    for repo, rs in summary["repos"].items():
        print(f"\n===== {repo}  {args.since}..{args.until}  (api calls: {client.calls})")
        print(f"{'workflow':44s} {'runs':>6s} {'costed':>6s} {'linux':>8s} {'slim':>8s} {'self-h':>8s} {'$est':>8s}")
        for fn, w in sorted(rs["workflows"].items(), key=lambda kv: -kv[1]["est_cost_usd"]):
            bm = w["billed_min_scaled"]
            flag = "!" if w["incomplete"] else ("~" if w["sampled"] else " ")
            print(f"{flag}{fn:43s} {w['runs_total']:>6d} {w['runs_costed']:>6d} "
                  f"{bm.get('linux',0):>8,.0f} {bm.get('slim',0):>8,.0f} {bm.get('self-hosted',0):>8,.0f} "
                  f"{w['est_cost_usd']:>8.2f}")
        inc = "  [INCOMPLETE: day listings capped at 1000 — undercounts]" if rs["totals"].get("incomplete_workflows") else ""
        print(f"TOTAL ${rs['totals']['est_cost_usd']:.2f}  pushes/day: {rs['pushes_by_day']}{inc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
