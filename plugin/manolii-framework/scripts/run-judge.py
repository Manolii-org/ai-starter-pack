#!/usr/bin/env python3
"""
Stage 3 final filter for PR assessment. Reads candidate JSON files from all
specialist agents, applies 3-gate filter (Accuracy + Actionability + Novelty),
and posts a consolidated PR review to GitHub.

Usage:
  python3 scripts/run-judge.py \
    --candidates-dir .ai/candidates/ \
    --pr-number ${{ github.event.pull_request.number }} \
    --sha ${{ github.event.pull_request.head.sha }}
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Hidden marker stamped into every posted review, used for idempotency.
REVIEW_MARKER = "<!-- pr-assessment-v1 -->"

_MD_UNSAFE_CHARS = re.compile(r"([\\`*_\[\]()<>#|~!])")


def _markdown_safe(text: str) -> str:
    """Neutralise Markdown syntax and @mentions in PR-derived text.

    danger_reason is shaped by untrusted diff content; unescaped Markdown can
    alter the displayed assessment or ping unintended users.
    """
    text = _MD_UNSAFE_CHARS.sub(r"\\\1", text)
    return text.replace("@", "@\u200b")


MAX_REASON_LEN = 300
# The judge posts through the Actions GITHUB_TOKEN, so its reviews are authored by
# github-actions[bot] — an identity a PR author cannot forge, unlike the marker text.
# Both must match before a review counts as "the judge already spoke for this SHA".
JUDGE_REVIEW_AUTHOR = "github-actions[bot]"
# GitHub caps per_page at 100; the page cap is a runaway guard, not a real limit.
_REVIEWS_PER_PAGE = 100
_MAX_REVIEW_PAGES = 100


class Finding:
    """Represents a single PR assessment finding."""

    def __init__(
        self,
        source: str,
        file: str,
        line: Optional[int],
        severity: str,
        message: str,
        fix: str,
        confidence: str = "medium",
        finding_id: Optional[str] = None,
    ):
        self.source = source
        self.file = file
        self.line = line
        self.severity = severity
        self.message = message
        self.fix = fix
        self.confidence = confidence
        # Synthesize ID if not provided
        self.finding_id = (
            finding_id
            or f"{source}-{file}-{line or 'null'}"
        )

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "source": self.source,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "message": self.message,
            "fix": self.fix,
            "confidence": self.confidence,
        }


class Judge:
    """PR assessment judge: loads candidates, applies filters, posts review."""

    def __init__(
        self,
        candidates_dir: str,
        pr_number: int,
        sha: str,
    ):
        self.candidates_dir = Path(candidates_dir)
        self.pr_number = pr_number
        self.sha = sha
        self.repo = os.getenv("GITHUB_REPOSITORY", "")
        self.token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        self.merge_danger = self._load_merge_danger()
        # An `edited` rerun at the same HEAD must publish a fresh verdict: dedup
        # keys on commit + metadata digest so a title/body edit re-posts. The
        # base SHA is part of the key: a base update changes the merge diff the
        # judge evaluated, so the old verdict must not suppress the fresh one.
        # The classified door/blast verdict is keyed too — a rerun that recovers
        # from `unknown` to a real verdict must overwrite, not be suppressed.
        meta_src = (
            os.getenv("PR_TITLE", "")
            + "\0"
            + os.getenv("PR_BODY", "")
            + "\0"
            + os.getenv("PR_BASE_SHA", "")
            + "\0"
            + str(self.merge_danger.get("door", ""))
            + "\0"
            + str(self.merge_danger.get("blast_radius", ""))
        )
        self.meta_digest = hashlib.sha256(meta_src.encode()).hexdigest()[:12]
        self.judge_log_dir = Path(".ai/judge-log")

    @staticmethod
    def _load_merge_danger() -> dict:
        """Read the classifier's merge-danger verdict from the classify job output.

        'unknown'/absent means unclassified — never rendered as a safe verdict.
        """
        try:
            data = json.loads(os.getenv("MERGE_DANGER", "{}") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def load_candidates(self) -> list[Finding]:
        """Load all findings from .ai/candidates/*.json (skip manifest.json)."""
        findings: list[Finding] = []

        if not self.candidates_dir.exists():
            logger.warning(f"Candidates directory not found: {self.candidates_dir}")
            return findings

        for candidate_file in sorted(self.candidates_dir.glob("*.json")):
            if candidate_file.name == "manifest.json":
                continue

            try:
                with open(candidate_file) as f:
                    data = json.load(f)

                source = data.get("source", candidate_file.stem)
                candidate_findings = data.get("findings", [])

                for raw_finding in candidate_findings:
                    finding = Finding(
                        source=source,
                        file=raw_finding.get("file", ""),
                        line=raw_finding.get("line"),
                        severity=raw_finding.get("severity", "WARNING"),
                        message=raw_finding.get("message", ""),
                        fix=raw_finding.get("fix", ""),
                        confidence=raw_finding.get("confidence", "medium"),
                        finding_id=raw_finding.get("finding_id"),
                    )
                    findings.append(finding)
                    logger.info(
                        f"Loaded finding from {source}: {finding.file}:{finding.line}"
                    )

            except Exception as e:
                logger.error(f"Failed to load {candidate_file}: {e}")

        return findings

    def is_vague_fix(self, fix: str) -> bool:
        """
        Check if a fix is vague (less than 20 chars, no file:line, no backticks).
        Vague phrases: add, fix, update, improve, check, ensure, consider,
        review, handle, include, use (followed by generic object).
        """
        if not fix or len(fix) < 20:
            return True

        vague_pattern = (
            r"\b(add|fix|update|improve|check|ensure|consider|review|handle|include|use)\b\s+"
            r"(error\s+handling|tests|validation|the\s+code|this|it)\b"
        )

        has_code_ref = ":" in fix or "`" in fix
        matches_vague = bool(re.search(vague_pattern, fix, re.IGNORECASE))

        return matches_vague and not has_code_ref

    def apply_specificity_gate(self, findings: list[Finding]) -> list[Finding]:
        """
        Gate 4 (Specificity) — backstop enforcement.
        Drop findings where fix field is None/empty, <20 chars, or vague.
        """
        filtered = []
        dropped = []

        for finding in findings:
            if not finding.fix:
                dropped.append((finding, "empty_fix"))
            elif self.is_vague_fix(finding.fix):
                dropped.append((finding, "vague_fix"))
            else:
                filtered.append(finding)

        for finding, reason in dropped:
            logger.info(
                f"Dropped {finding.finding_id} (specificity): {reason}"
            )

        return filtered

    def get_judge_system_prompt(self) -> str:
        """Return the judge agent's system prompt."""
        return """You are the final gatekeeper for PR assessment findings. Your job is to apply
three filters: Accuracy (is the claim verifiable?), Actionability (is there a concrete fix?),
and Novelty (is this a duplicate or already caught by CI?).

You will receive a JSON array of findings from multiple specialist agents and tools.
For each finding, decide whether to keep it (post to GitHub) or drop it.

Return a JSON object with exactly this structure:
{
  "surviving": [
    {"finding_id": "...", "source": "...", "file": "...", "line": ...,
     "severity": "ERROR|WARNING", "message": "...", "fix": "..."}
  ],
  "dropped_count": <int>,
  "review_action": "REQUEST_CHANGES|COMMENT"
}

Apply all three gates strictly. Only include findings in "surviving" that pass all gates.
If any ERROR survives, set review_action to "REQUEST_CHANGES"; otherwise "COMMENT".
"""

    def invoke_judge_agent(self, findings: list[Finding]) -> Optional[dict]:
        """
        Invoke the judge agent via Claude API.
        Returns parsed judge response or None on failure.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not set; cannot invoke judge agent")
            return None

        # Build the user message with findings in untrusted_candidates tags
        findings_json = json.dumps(
            [f.to_dict() for f in findings],
            indent=2,
        )
        user_message = f"""Apply the 3-gate filter to these findings and return your decision.

<untrusted_candidates>
{findings_json}
</untrusted_candidates>

Remember: pass all three gates or drop the finding. Return only valid JSON, no markdown fences."""

        # Call Claude API
        try:
            request_body = {
                "model": "claude-sonnet-4-6",
                "max_tokens": 4000,
                "system": self.get_judge_system_prompt(),
                "messages": [
                    {"role": "user", "content": user_message}
                ],
            }

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(request_body).encode("utf-8"),
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )

            logger.info("Invoking judge agent...")
            with urllib.request.urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))

            # Reasoning models (e.g. DeepSeek via a proxy alias) prepend a
            # `thinking` block — select text-type blocks, not content[0].
            response_text = "".join(
                b.get("text", "")
                for b in result.get("content", [])
                if isinstance(b, dict) and b.get("type", "text") == "text"
            )
            if not response_text:
                raise KeyError("no text blocks in judge response content")
            logger.info(f"Judge response: {response_text[:200]}...")

            # Parse JSON, stripping markdown fences if present
            json_text = response_text
            if "```json" in json_text:
                json_text = json_text.split("```json")[1].split("```")[0]
            elif "```" in json_text:
                json_text = json_text.split("```")[1].split("```")[0]

            judge_result = json.loads(json_text.strip())
            return judge_result

        except urllib.error.URLError as e:
            logger.error(f"Judge API call failed: {e}")
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse judge response: {e}")
            return None

    def post_review_to_github(
        self,
        surviving: list[dict],
        review_action: str,
    ) -> bool:
        """Post the consolidated review to GitHub via REST API."""
        if not self.token or not self.repo:
            logger.warning(
                "GITHUB_TOKEN or GITHUB_REPOSITORY not set; skipping GitHub post"
            )
            return False

        # Dedup is outcome-keyed: a prior CLEAN verdict must not suppress a
        # rerun that now has findings (e.g. a specialist timed out first time).
        if self._review_exists_at_sha("findings"):
            logger.info(f"Review already posted at {self.sha[:8]}; skipping")
            return True

        # Format review body
        body_lines = [
            REVIEW_MARKER,
            f"<!-- meta:{self.meta_digest}:findings -->",
            "## PR Assessment Review",
        ]

        door = self.merge_danger.get("door")
        if door in ("one-way", "two-way"):
            blast = self.merge_danger.get("blast_radius", "unknown")
            line = f"**Merge danger:** {door} door · blast radius: {blast}"
            reason = _markdown_safe(
                str(self.merge_danger.get("danger_reason", "")).replace("\n", " ").strip()[:MAX_REASON_LEN]
            )
            if reason:
                line += f" — {reason}"
        else:
            # An unclassified verdict must still surface — omitting the line
            # would make a classifier failure indistinguishable from a clean
            # low-risk assessment.
            line = "**Merge danger:** unknown (unclassified)"
        body_lines.append(line)
        body_lines.append("")

        errors = [f for f in surviving if f["severity"] == "ERROR"]
        warnings = [f for f in surviving if f["severity"] == "WARNING"]

        body_lines.append(f"**{len(errors)} ERROR(s)** / **{len(warnings)} WARNING(s)**")
        body_lines.append("---")

        for finding in sorted(
            surviving,
            key=lambda f: (f["severity"] == "WARNING", f["file"], f["line"] or 0),
        ):
            severity = finding["severity"]
            file_ref = f"{_markdown_safe(str(finding['file']))}"
            if finding["line"]:
                file_ref += f":{finding['line']}"

            # Finding fields are specialist prose shaped by untrusted diff
            # content — escape Markdown so crafted text can't forge sections,
            # links, or @mentions in the posted review.
            body_lines.append(f"### [{severity}] {file_ref}")
            body_lines.append(f"**Issue:** {_markdown_safe(str(finding['message']))}")
            body_lines.append(f"**Fix:** {_markdown_safe(str(finding['fix']))}")
            body_lines.append(f"**Source:** {_markdown_safe(str(finding['source']))}")
            body_lines.append("")

        review_body = "\n".join(body_lines)

        # Post review
        try:
            request_body = {
                "body": review_body,
                "event": review_action,
                "comments": [],  # Individual comments not needed with body
            }

            url = (
                f"https://api.github.com/repos/{self.repo}/pulls/"
                f"{self.pr_number}/reviews"
            )
            req = urllib.request.Request(
                url,
                data=json.dumps(request_body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            logger.info(f"Posting review to {url}...")
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
                review_id = result.get("id", "unknown")
                logger.info(f"Review posted successfully (ID: {review_id})")
                return True

        except urllib.error.URLError as e:
            logger.error(f"Failed to post review: {e}")
            return False

    def _review_exists_at_sha(self, kind: str) -> bool:
        """Check if a review by the judge with the marker already exists at this SHA.

        Requires BOTH the marker AND the judge's author identity. The marker is public
        text — anyone who can review the PR can paste it, and a PR that discusses this
        file contains it. Matching on the marker alone lets an unrelated review suppress
        a genuine judge verdict, which is a silent loss: the assessment reports success
        and posts nothing.

        PAGINATES. The reviews endpoint returns 30 per page by default, oldest-first,
        and the review we care about is the newest. Unpaginated, any PR with more than
        30 reviews stops matching its own earlier verdict and posts a duplicate on
        every re-run.
        """
        if not self.token or not self.repo:
            return False

        latest_digest_matches = None
        for page in range(1, _MAX_REVIEW_PAGES + 1):
            try:
                url = (
                    f"https://api.github.com/repos/{self.repo}/pulls/"
                    f"{self.pr_number}/reviews"
                    f"?per_page={_REVIEWS_PER_PAGE}&page={page}"
                )
                req = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    method="GET",
                )

                with urllib.request.urlopen(req, timeout=10) as response:
                    reviews = json.loads(response.read().decode("utf-8"))
            except Exception as e:
                # Fail open, as before: a lookup failure must not block the verdict.
                logger.error(f"Failed to check existing reviews (page {page}): {e}")
                return False

            if not isinstance(reviews, list):
                logger.error("Unexpected reviews response shape — skipping duplicate check")
                return False

            for review in reviews:
                author = (review.get("user") or {}).get("login")
                body = review.get("body") or ""
                if (
                    review.get("commit_id") == self.sha
                    and author == JUDGE_REVIEW_AUTHOR
                    and REVIEW_MARKER in body
                ):
                    # Reviews are returned oldest-first; only the LATEST judge
                    # review at this commit decides dedup. The tracker must live
                    # across pages — returning True on a page-1 match would let
                    # a stale digest suppress a newer contradicting verdict that
                    # sits on a later page (A→B→A across the page boundary).
                    latest_digest_matches = f"<!-- meta:{self.meta_digest}:{kind} -->" in body

            if len(reviews) < _REVIEWS_PER_PAGE:
                return bool(latest_digest_matches)

        logger.error(f"Hit the {_MAX_REVIEW_PAGES}-page cap scanning reviews")
        return False

    def write_log_entry(
        self,
        finding_id: str,
        decision: str,
        gate: str,
        reason: str,
    ) -> None:
        """Write a decision log entry."""
        self.judge_log_dir.mkdir(parents=True, exist_ok=True)

        log_file = (
            self.judge_log_dir / f"{self.pr_number}-{self.sha[:8]}.jsonl"
        )

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "finding_id": finding_id,
            "decision": decision,
            "gate": gate,
            "reason": reason,
        }

        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def run(self) -> int:
        """Main entry point."""
        logger.info(
            f"Judge running for PR #{self.pr_number} at {self.sha[:8]}..."
        )

        # Ensure log dir exists
        self.judge_log_dir.mkdir(parents=True, exist_ok=True)

        # Load candidates
        findings = self.load_candidates()
        logger.info(f"Loaded {len(findings)} findings from candidates")

        if not findings:
            logger.info("No findings to process; posting advisory comment")
            self._post_no_findings_comment()
            return 0

        # Apply specificity gate (programmatic backstop)
        findings = self.apply_specificity_gate(findings)
        logger.info(f"{len(findings)} findings remain after specificity gate")

        if not findings:
            logger.info("All findings dropped by specificity gate")
            self._post_no_findings_comment()
            return 0

        # Invoke judge agent for 3-gate filter
        judge_result = self.invoke_judge_agent(findings)

        if not judge_result:
            logger.error("Judge agent invocation failed")
            self._post_advisory_warning()
            return 0

        # Validate and sanitise judge output before use
        surviving = judge_result.get("surviving", [])
        if not isinstance(surviving, list):
            surviving = []
        surviving = [f for f in surviving if isinstance(f, dict)]

        dropped_count = judge_result.get("dropped_count", 0)
        if not isinstance(dropped_count, int):
            try:
                dropped_count = int(dropped_count)
            except (TypeError, ValueError):
                dropped_count = 0

        review_action = judge_result.get("review_action", "COMMENT")
        if review_action not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
            review_action = "COMMENT"

        logger.info(
            f"Judge result: {len(surviving)} surviving, "
            f"{dropped_count} dropped, action={review_action}"
        )

        # Log all decisions (surviving + dropped)
        all_findings = findings
        surviving_ids = {f["finding_id"] for f in surviving}

        for finding in all_findings:
            if finding.finding_id in surviving_ids:
                self.write_log_entry(
                    finding.finding_id,
                    "post",
                    "passed",
                    "Passed all 3 gates",
                )
            else:
                self.write_log_entry(
                    finding.finding_id,
                    "drop",
                    "gate",
                    "Dropped by judge agent",
                )

        # Post review (if findings survive and token available)
        if surviving:
            if not self.post_review_to_github(surviving, review_action):
                logger.warning("Failed to post review to GitHub")
        else:
            logger.info("No findings survived filters; skipping GitHub post")
            self._post_no_findings_comment()

        return 0

    def _post_no_findings_comment(self) -> None:
        """Post a COMMENT noting no findings (idempotent — skips if already posted)."""
        if not self.token or not self.repo:
            logger.info("No findings; skipping GitHub post (no token/repo)")
            return

        if self._review_exists_at_sha("clean"):
            logger.info("No-findings comment already posted for this SHA — skipping")
            return

        try:
            body_lines = [
                REVIEW_MARKER,
                f"<!-- meta:{self.meta_digest}:clean -->",
                "## PR Assessment",
            ]
            door = self.merge_danger.get("door")
            if door in ("one-way", "two-way"):
                blast = self.merge_danger.get("blast_radius", "unknown")
                line = f"**Merge danger:** {door} door · blast radius: {blast}"
                reason = _markdown_safe(
                    str(self.merge_danger.get("danger_reason", "")).replace("\n", " ").strip()[:MAX_REASON_LEN]
                )
                if reason:
                    line += f" — {reason}"
            else:
                line = "**Merge danger:** unknown (unclassified)"
            body_lines.append(line)
            body_lines.append("")
            body_lines.append("No actionable findings produced by specialist agents.")
            request_body = {
                "body": "\n".join(body_lines),
                "event": "COMMENT",
            }

            url = (
                f"https://api.github.com/repos/{self.repo}/pulls/"
                f"{self.pr_number}/reviews"
            )
            req = urllib.request.Request(
                url,
                data=json.dumps(request_body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30):
                logger.info("No-findings comment posted")

        except Exception as e:
            logger.warning(f"Failed to post no-findings comment: {e}")

    def _post_advisory_warning(self) -> None:
        """Post an advisory WARNING when judge fails."""
        if not self.token or not self.repo:
            logger.info("Judge failed; no token/repo for advisory post")
            return

        try:
            request_body = {
                "body": (
                    f"{REVIEW_MARKER}\n"
                    "## PR Assessment\n"
                    "⚠️ Assessment system encountered an error. "
                    "Manual review recommended.\n\n"
                    "**Merge danger:** unknown (assessment error)"
                ),
                "event": "COMMENT",
            }

            url = (
                f"https://api.github.com/repos/{self.repo}/pulls/"
                f"{self.pr_number}/reviews"
            )
            req = urllib.request.Request(
                url,
                data=json.dumps(request_body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30):
                logger.info("Advisory warning posted")

        except Exception as e:
            logger.warning(f"Failed to post advisory warning: {e}")


def main() -> int:
    """Parse arguments and run judge."""
    parser = argparse.ArgumentParser(
        description="Stage 3 PR assessment judge."
    )
    parser.add_argument(
        "--candidates-dir",
        default=".ai/candidates/",
        help="Directory containing candidate JSON files",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        required=True,
        help="GitHub PR number",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub repo in owner/name format (defaults to GITHUB_REPOSITORY env)",
    )
    parser.add_argument(
        "--sha",
        default=os.environ.get("GITHUB_SHA", ""),
        help="Head SHA for duplicate-review detection (defaults to GITHUB_SHA env; required)",
    )

    args = parser.parse_args()

    sha = args.sha.strip()
    if not sha:
        parser.error("--sha or GITHUB_SHA is required for duplicate-review detection")

    # --repo overrides GITHUB_REPOSITORY env so the workflow can pass it explicitly
    if args.repo:
        os.environ["GITHUB_REPOSITORY"] = args.repo

    judge = Judge(
        candidates_dir=args.candidates_dir,
        pr_number=args.pr_number,
        sha=sha,
    )

    return judge.run()


if __name__ == "__main__":
    sys.exit(main())
