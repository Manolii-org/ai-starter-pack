#!/usr/bin/env python3
"""Observation-only Jev (TypeSafe System One) shadow for the pack Judge.

Re-asks Judge's four per-finding gates (accuracy, actionability, novelty,
specificity) as Noul questions against a pinned Jev model and records whether
Jev would have kept each finding. It never changes Judge's verdict, review,
or exit status: every refusal, provider error, or budget expiry is a
payload-free receipt and the process always exits 0.

Activation is literal and layered (see docs/jev-judge-shadow.md):
  - JEV_ENABLED_JUDGE_FINDING_SHADOW must be exactly "1"
  - JEV_SHADOW_ENTITY must name the caller's own billing entity
  - TYPESAFE_API_KEY must be that entity's scoped credential
  - CLIENT_AI_POLICY must be empty (client engagements stay off)
  - the repository owner must not be on DENIED_OWNERS
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
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

FLAG_NAME = "JEV_ENABLED_JUDGE_FINDING_SHADOW"
MODEL = "jev-1.13.0"
POLICY_VERSION = "pack-judge-shadow-v1"
SCHEMA_VERSION = 1
SURFACE = "pack_judge_finding_shadow"
NOUL_THRESHOLD = 0.5
MAX_FINDINGS = 25
MAX_SHADOW_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1_000_000
EXCERPT_RADIUS = 20
MAX_STATE_CHARS = 20_000
DEFAULT_BASE_URL = "https://api.typesafe.ai"
ENTITY_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
# Orgs whose product/CI AI policy forbids this third-party call regardless of
# caller inputs. CPDcheck keeps all AI on Azure OpenAI (australiaeast).
DENIED_OWNERS = frozenset({"cpdcheck"})

_TOKEN_SHAPES = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bdp\.(?:st|pt|sa|ct|scim)\.[A-Za-z0-9._-]{16,}"),
    re.compile(
        r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|password|passwd))"
        r"(\s*[:=]\s*)(['\"]?)[^\s'\"]{8,}\3"
    ),
]


def judge_shadow_questions() -> dict[str, dict[str, str]]:
    return {
        "accuracy": {
            "type": "noul",
            "instructions": (
                "Is the finding factually supported by the supplied evidence excerpt? "
                "Treat absent or unavailable evidence as uncertainty, not contradiction."
            ),
        },
        "actionability": {
            "type": "noul",
            "instructions": "Does the finding identify a change a developer can take?",
        },
        "novelty": {
            "type": "noul",
            "instructions": (
                "Is this a newly introduced issue rather than only a general or "
                "already-documented pattern?"
            ),
        },
        "specificity": {
            "type": "noul",
            "instructions": (
                "Does the proposed fix name a specific location or give a concrete code change?"
            ),
        },
    }


def redact(text: str) -> str:
    for pattern in _TOKEN_SHAPES[:-1]:
        text = pattern.sub("[REDACTED]", text)
    return _TOKEN_SHAPES[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def refusal_reason(env: Mapping[str, str]) -> str | None:
    """Return why the shadow must not call the provider, or None to proceed."""

    if env.get(FLAG_NAME, "0") != "1":
        return "flag_off"
    if env.get("GITHUB_REPOSITORY_OWNER", "").strip().lower() in DENIED_OWNERS:
        return "owner_denied"
    if env.get("CLIENT_AI_POLICY", "").strip():
        return "client_ai_policy"
    if not ENTITY_PATTERN.fullmatch(env.get("JEV_SHADOW_ENTITY", "")):
        return "entity_unset"
    if not env.get("TYPESAFE_API_KEY", ""):
        return "credential_unavailable"
    return None


def _finding_id(source: str, item: Mapping[str, Any]) -> str:
    # Mirrors run-judge.py Finding: producer id wins, else the same digest.
    given = item.get("finding_id")
    if given:
        return str(given)
    identity = (
        f"{source}|{item.get('file', '')}|{item.get('line')}|{item.get('message', '')}"
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


def load_findings(candidates_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not candidates_dir.is_dir():
        return findings
    for path in sorted(candidates_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        source = str(data.get("source", path.stem))
        for item in data.get("findings", []) or []:
            if not isinstance(item, dict):
                continue
            findings.append(
                {
                    "finding_id": _finding_id(source, item),
                    "file": str(item.get("file", "") or ""),
                    "line": item.get("line")
                    if isinstance(item.get("line"), int)
                    else None,
                    "severity": str(item.get("severity", "") or ""),
                    "message": str(item.get("message", "") or ""),
                    "fix": str(item.get("fix", "") or ""),
                }
            )
    return findings


def load_decisions(judge_log_dir: Path, pr_number: int, sha: str) -> dict[str, bool]:
    """Map finding_id -> Judge kept it, from run-judge.py's decision log."""

    decisions: dict[str, bool] = {}
    path = judge_log_dir / f"{pr_number}-{sha[:8]}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return decisions
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("decision") in {"post", "drop"}:
            decisions[str(entry.get("finding_id"))] = entry["decision"] == "post"
    return decisions


def _excerpt(workspace: Path, rel: str, line: int | None) -> tuple[str, str]:
    if not rel or rel.startswith("/") or "\x00" in rel:
        return "unavailable", ""
    root = workspace.resolve()
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        return "unavailable", ""
    if not target.is_file():
        return "absent", ""
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "unavailable", ""
    centre = line if line and line > 0 else 1
    start = max(centre - EXCERPT_RADIUS, 1)
    chunk = lines[start - 1 : centre + EXCERPT_RADIUS]
    return "present", "\n".join(f"{start + i}: {text}" for i, text in enumerate(chunk))


def provider_state(finding: Mapping[str, Any], head_sha: str, workspace: Path) -> str:
    status, evidence = _excerpt(workspace, finding["file"], finding["line"])
    state = {
        "finding_id": finding["finding_id"],
        "head_sha": head_sha[:64],
        "severity": finding["severity"],
        "message": finding["message"],
        "fix": finding["fix"],
        "location": {"file": finding["file"], "line": finding["line"]},
        "evidence_status": status,
        "evidence": evidence,
    }
    return redact(json.dumps(state, sort_keys=True, separators=(",", ":")))[
        :MAX_STATE_CHARS
    ]


class ShadowError(RuntimeError):
    """Provider call failed; only the class name is ever recorded."""


class ShadowHTTPError(ShadowError):
    pass


class ShadowResponseError(ShadowError):
    pass


class ShadowTransportError(ShadowError):
    pass


def evaluate(
    state: str,
    env: Mapping[str, str],
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, float]:
    base = (env.get("TYPESAFE_BASE_URL", "") or DEFAULT_BASE_URL).strip().rstrip("/")
    if not base.startswith("https://"):
        raise ShadowTransportError("TYPESAFE_BASE_URL must be https")
    questions = judge_shadow_questions()
    request = urllib.request.Request(
        f"{base}/v1/systemone",
        data=json.dumps(
            {"state": state, "model": MODEL, "questions": questions},
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + env["TYPESAFE_API_KEY"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ShadowHTTPError(str(exc.code)) from None
    except (OSError, urllib.error.URLError):
        raise ShadowTransportError("request failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ShadowResponseError("response too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ShadowResponseError("invalid json") from None
    if not isinstance(data, dict) or data.get("model") != MODEL:
        raise ShadowResponseError("model mismatch")
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ShadowResponseError("answer ids mismatch")
    out: dict[str, float] = {}
    for qid in questions:
        answer = answers[qid]
        value = answer.get("noul") if isinstance(answer, dict) else None
        if (
            not isinstance(answer, dict)
            or answer.get("type") != "noul"
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ShadowResponseError("invalid noul answer")
        out[qid] = float(value)
    return out


def run_shadow(
    *,
    candidates_dir: Path,
    judge_log_dir: Path,
    workspace: Path,
    pr_number: int,
    head_sha: str,
    env: Mapping[str, str],
    opener: Callable[..., Any] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str | None, list[dict[str, Any]]]:
    reason = refusal_reason(env)
    if reason:
        return reason, []
    decisions = load_decisions(judge_log_dir, pr_number, head_sha)
    if not decisions:
        return "no_judge_decisions", []
    findings = [
        f for f in load_findings(candidates_dir) if f["finding_id"] in decisions
    ]
    seen: set[str] = set()
    receipts: list[dict[str, Any]] = []
    run_started = clock()
    for finding in findings:
        if finding["finding_id"] in seen:
            continue
        seen.add(finding["finding_id"])
        kept = decisions[finding["finding_id"]]
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": str(uuid.uuid4()),
            "surface": SURFACE,
            "entity": env["JEV_SHADOW_ENTITY"],
            "repository": env.get("GITHUB_REPOSITORY", ""),
            "pr_number": pr_number,
            "head_sha": head_sha,
            "finding_id": finding["finding_id"],
            "pinned_model": MODEL,
            "policy_version": POLICY_VERSION,
            "thresholds": {"all_nouls_min": NOUL_THRESHOLD},
            "judge_kept": kept,
            "action": "observe_only",
            "override": "judge_unchanged",
            "eval_attempted": False,
        }
        started = clock()
        try:
            if len(receipts) >= MAX_FINDINGS:
                raise TimeoutError("finding budget exhausted")
            if started - run_started >= MAX_SHADOW_SECONDS:
                raise TimeoutError("time budget exhausted")
            state = provider_state(finding, head_sha, workspace)
            receipt["eval_attempted"] = True
            probabilities = evaluate(state, env, opener=opener)
            would_keep = all(v >= NOUL_THRESHOLD for v in probabilities.values())
            receipt["probabilities"] = probabilities
            receipt["shadow_class"] = (
                "agreement_keep"
                if kept and would_keep
                else "false_drop_candidate"
                if kept
                else "false_keep_candidate"
                if would_keep
                else "agreement_drop"
            )
        except Exception as exc:  # noqa: BLE001 — observation must never affect Judge.
            receipt["shadow_class"] = "unavailable"
            receipt["error_class"] = type(exc).__name__
        receipt["latency_ms"] = round((clock() - started) * 1000, 1)
        receipts.append(receipt)
    return None, receipts


def _summary(reason: str | None, receipts: list[dict[str, Any]]) -> str:
    if reason:
        return f"Jev judge shadow: not run ({reason}). Judge verdict unaffected."
    counts: dict[str, int] = {}
    for r in receipts:
        counts[r["shadow_class"]] = counts.get(r["shadow_class"], 0) + 1
    detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    return (
        f"Jev judge shadow ({MODEL}, observe-only): observed={len(receipts)} {detail}. "
        "Judge verdict unaffected."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidates-dir", type=Path, default=Path(".ai/candidates"))
    parser.add_argument("--judge-log-dir", type=Path, default=Path(".ai/judge-log"))
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, default=Path("judge-jev-shadow.jsonl"))
    args = parser.parse_args(argv)
    try:
        reason, receipts = run_shadow(
            candidates_dir=args.candidates_dir,
            judge_log_dir=args.judge_log_dir,
            workspace=args.workspace,
            pr_number=args.pr_number,
            head_sha=args.sha,
            env=os.environ,
        )
        if receipts:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("a", encoding="utf-8") as handle:
                for receipt in receipts:
                    handle.write(json.dumps(receipt, sort_keys=True) + "\n")
        line = _summary(reason, receipts)
    except Exception as exc:  # noqa: BLE001 — fail open: never fail the Judge job.
        line = f"Jev judge shadow: internal error ({type(exc).__name__}). Judge verdict unaffected."
    print(f"[jev-judge-shadow] {line}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
