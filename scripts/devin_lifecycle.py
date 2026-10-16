#!/usr/bin/env python3
"""Fail-closed, leaf-only Cloud session archival. No deletion or termination.

Provider contract verified 2026-10-16:
https://docs.devin.ai/api-reference/v3/sessions/organizations-sessions
https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session
https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session-messages
https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions-archive
The grace interval is quiet time since *any* session update, not runtime.
"""

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
from pathlib import Path

MARKER = "DEVIN_LIFECYCLE_V1 "
PROTECTED = {"retain", "pinned", "keep", "needs-human", "attention", "lifecycle:retain", "manual-review", "manual_review", "primary-blocker", "escalation", "blocked", "needs-attention", "lifecycle:attention"}


def canonical_session_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:devin-)?[a-zA-Z0-9]{1,128}", value):
        raise ValueError("invalid session ID")
    return value.removeprefix("devin-")


def outcome(messages):
    """Only the authenticated agent's final message can declare a terminal result."""
    if not messages or messages[-1].get("source") != "devin":
        return None
    body = messages[-1].get("message")
    if not isinstance(body, str):
        return None
    lines = body.splitlines()
    if not lines or not lines[-1].startswith(MARKER):
        return None
    try:
        receipt = json.loads(lines[-1][len(MARKER):])
    except (ValueError, TypeError):
        return None
    if not isinstance(receipt, dict) or type(receipt.get("version")) is not int or receipt["version"] != 1:
        return None
    return receipt


def decision(session, messages, policy, now):
    """Return a stable exclusion reason, or 'eligible'. Missing data is not success."""
    automation_id = session.get("automation_id")
    if session.get("origin") != "automation" or not isinstance(automation_id, str) or not re.fullmatch(r"auto-[a-zA-Z0-9]+", automation_id) or automation_id not in policy["automation_ids"]:
        return "not_managed"
    sid = canonical_session_id(session.get("session_id"))
    if sid in {canonical_session_id(value) for value in policy.get("protected_session_ids", [])}:
        return "protected"
    if session.get("is_archived") is not False:
        return "archived_or_unknown"
    if session.get("status") != "suspended" or session.get("status_detail") != "inactivity":
        return "not_quiet"
    updated = session.get("updated_at")
    if type(updated) is not int or updated > now or now - updated < policy["grace_seconds"]:
        return "grace_period"
    tags = session.get("tags")
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        return "protection_unknown"
    if any(t.lower() in PROTECTED or t.lower().startswith(("retain:", "lifecycle:retain:")) for t in tags):
        return "retained"
    # Refuse ALL parents: archive cascades, so even a quiet parent is unsafe.
    if session.get("child_session_ids") != []:
        return "descendants_or_unknown"
    prs = session.get("pull_requests")
    if not isinstance(prs, list) or any(not isinstance(p, dict) or p.get("pr_state") not in {"closed", "merged", "CLOSED", "MERGED"} for p in prs):
        return "pr_followthrough"
    if not messages:
        return "messages_unknown"
    for index, message in enumerate(messages):
        if message.get("source") not in {"user", "devin"}:
            return "messages_unknown"
        if message["source"] == "user":
            created = message.get("created_at")
            started = session.get("created_at")
            launch_id = next((v for k, v in policy.get("launch_event_ids", {}).items() if canonical_session_id(k) == sid), None)
            trusted_launch = message.get("origin") == "automation" or (isinstance(launch_id, str) and message.get("event_id") == launch_id)
            if index != 0 or not trusted_launch or type(created) is not int or type(started) is not int or not 0 <= created - started <= 120:
                return "human_followup"
    receipt = outcome(messages)
    if not receipt:
        return "outcome_unknown"
    if receipt.get("outcome") not in {"delivered", "noop"}:
        return "attention"
    if any(receipt.get(k) is not False for k in ("retain", "needs_attention", "pending_work")):
        return "attention_or_unknown"
    if receipt.get("delivery_verified") is not True:
        return "delivery_unknown"
    return "eligible"


def fingerprint(session, messages):
    raw = json.dumps([session, messages], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class Client:
    def __init__(self, org_id, token):
        if not re.fullmatch(r"org-[a-zA-Z0-9]+", org_id):
            raise ValueError("invalid organization ID")
        self.base = f"https://api.devin.ai/v3/organizations/{org_id}/sessions"
        self.token = token

    def request(self, suffix="", method="GET"):
        request = urllib.request.Request(self.base + suffix, method=method, headers={"Authorization": "Bearer " + self.token})
        # Do not forward credentials across redirects or retry ambiguous writes.
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    def pages(self, suffix="", limit=1000, filters=None):
        items, after, seen = [], None, set()
        while True:
            query = {**(filters or {}), "first": min(100, limit - len(items))}
            if after:
                query["after"] = after
            page = self.request(suffix + "?" + urllib.parse.urlencode(query, doseq=True))
            if not isinstance(page.get("items"), list) or type(page.get("has_next_page")) is not bool:
                raise ValueError("incomplete pagination metadata")
            items.extend(page["items"])
            if len(items) > limit:
                raise ValueError("pagination bound exceeded")
            if not page["has_next_page"]:
                return items
            after = page.get("end_cursor")
            if not page["items"] or not isinstance(after, str) or not after or after in seen or len(items) >= limit:
                raise ValueError("pagination truncated or cursor repeated")
            seen.add(after)

    def read(self, session_id):
        identity = canonical_session_id(session_id)
        session = self.request("/" + session_id)
        if canonical_session_id(session.get("session_id")) != identity:
            raise ValueError("session identity mismatch")
        return session, self.pages("/" + session_id + "/messages", 1000)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_policy(policy):
    ids = policy.get("automation_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(s, str) or not re.fullmatch(r"auto-[a-zA-Z0-9]+", s) for s in ids):
        raise ValueError("explicit valid automation allowlist required")
    if not isinstance(policy.get("launch_event_ids", {}), dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in policy.get("launch_event_ids", {}).items()):
        raise ValueError("invalid trusted launch message IDs")
    protected = policy.get("protected_session_ids", [])
    if not isinstance(protected, list):
        raise TypeError("invalid protected session IDs")
    for sid in protected:
        canonical_session_id(sid)
    launch_ids = [canonical_session_id(sid) for sid in policy.get("launch_event_ids", {})]
    if len(set(launch_ids)) != len(launch_ids):
        raise ValueError("ambiguous launch session IDs")
    for key, low, high in [("grace_seconds", 3600, 604800), ("max_sessions", 1, 1000), ("max_archives", 1, 50)]:
        if type(policy.get(key)) is not int or not low <= policy[key] <= high:
            raise ValueError("invalid policy bound: " + key)


def sweep(client, policy, apply=False, reviewed=None, now=None, audit=None):
    validate_policy(policy)
    now = int(time.time()) if now is None else now
    report = {"version": 1, "generated_at": now, "apply": apply, "sessions": []}
    reviewed = reviewed or {}
    approved = {r["session_id"]: r for r in reviewed.get("sessions", []) if r.get("decision") == "eligible"}
    if apply and (type(reviewed.get("version")) is not int or reviewed["version"] != 1 or reviewed.get("apply") is not False or reviewed.get("policy") != policy or not 0 <= now - reviewed.get("generated_at", 0) <= 900):
        raise ValueError("apply needs a matching dry-run report less than 15 minutes old")
    report["policy"] = policy
    candidates = client.pages(limit=policy["max_sessions"], filters={"origins": ["automation"], "automation_ids": policy["automation_ids"], "is_archived": "false"})
    archived = 0
    for candidate in candidates:
        if candidate.get("origin") != "automation" or candidate.get("automation_id") not in policy["automation_ids"]:
            continue
        sid = candidate["session_id"]
        if apply and sid not in approved:
            continue
        session, messages = client.read(sid)
        reason = decision(session, messages, policy, now)
        digest = fingerprint(session, messages)
        row = {"session_id": sid, "decision": reason, "fingerprint": digest}
        report["sessions"].append(row)
        if apply and reason == "eligible" and archived < policy["max_archives"]:
            if digest != approved[sid]["fingerprint"]:
                row["decision"] = "changed_since_review"
                continue
            # The archive endpoint has no compare-and-swap. Recheck immediately;
            # there remains an unavoidable read/write race, not a safety guarantee.
            latest, latest_messages = client.read(sid)
            if fingerprint(latest, latest_messages) != digest or decision(latest, latest_messages, policy, int(time.time())) != "eligible":
                row["decision"] = "changed_before_archive"
                continue
            if not 0 <= int(time.time()) - reviewed["generated_at"] <= 900:
                raise ValueError("review expired before archive")
            if audit:
                audit({"event": "archive_attempt", **row})
            result = client.request("/" + sid + "/archive", "POST")
            if result.get("is_archived") is not True:
                raise ValueError("archive did not confirm is_archived")
            row["decision"] = "archived"
            archived += 1
            if audit:
                audit({"event": "archive_confirmed", **row})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reviewed-report", type=Path)
    args = parser.parse_args(argv)
    policy = json.loads(args.policy.read_text())
    try:
        validate_policy(policy)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    if args.apply and not args.reviewed_report:
        parser.error("--apply requires --reviewed-report")
    token = os.environ.get("DEVIN_API_KEY")
    if not token:
        parser.error("DEVIN_API_KEY must be injected at runtime")
    reviewed = json.loads(args.reviewed_report.read_text()) if args.reviewed_report else None
    try:
        report = sweep(Client(policy["org_id"], token), policy, args.apply, reviewed, audit=lambda row: print(json.dumps(row), file=sys.stderr, flush=True))
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        # Provider error bodies may contain private content; log only the class.
        print("Lifecycle sweep failed closed: " + type(exc).__name__, file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
