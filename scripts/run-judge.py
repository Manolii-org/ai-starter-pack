#!/usr/bin/env python3
"""
Stage 3 final filter for PR assessment. Reads candidate JSON files from all
specialist agents, applies 4-gate filter (Accuracy + Actionability + Novelty +
Specificity),
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
import urllib.parse
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

_ANTHROPIC_API_VERSION = "2023-06-01"
_ANTHROPIC_HOST = "api.anthropic.com"


def _proxy_transport_key() -> str:
    """Credential for a non-Anthropic endpoint.

    The proxy key may live under LLM_API_KEY, LITELLM_MASTER_KEY, or
    ANTHROPIC_API_KEY (legacy configs store the LiteLLM key under that name —
    no sk-ant credential is required). A genuine sk-ant-* key, though, is a
    first-party credential and must never be sent to a third-party host: if
    the resolved value has that shape, fail closed with "" so callers take
    their unconfigured path instead of leaking it.
    """
    for candidate in (
        os.environ.get("LLM_API_KEY"),
        os.environ.get("LITELLM_MASTER_KEY"),
        os.environ.get("ANTHROPIC_API_KEY"),
    ):
        if not candidate:
            continue
        key = candidate.strip()
        # The auth-scheme token is case-insensitive (RFC 7235).
        if key.lower() == "bearer" or key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key or key.lower().startswith("sk-ant-"):
            continue
        return key
    return ""


def _endpoint(direct: bool = False) -> tuple[str, str, bool]:
    """Resolve (api_key, url, proxied).

    Transport token: ANTHROPIC_API_KEY when calling Anthropic directly. When
    LITELLM_PROXY_URL or ANTHROPIC_BASE_URL points at a non-Anthropic host the
    request goes through that proxy with _proxy_transport_key().

    direct=True forces the Anthropic endpoint regardless of proxy config —
    used when adjudicating first_party findings (security review stays on a
    first-party model per docs/us-oss-eligibility-matrix.md). Under a
    configured proxy only ANTHROPIC_DIRECT_API_KEY authenticates there; the
    LiteLLM credential in ANTHROPIC_API_KEY must never cross the boundary.
    """
    base = (os.environ.get("LITELLM_PROXY_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    proxied = bool(base) and (urllib.parse.urlparse(base).hostname or "").lower().rstrip(".") != _ANTHROPIC_HOST
    if direct:
        key = os.environ.get("ANTHROPIC_DIRECT_API_KEY") or (
            os.environ.get("ANTHROPIC_API_KEY", "") if not proxied else ""
        )
        return key, _ANTHROPIC_API_URL, False
    if proxied:
        return _proxy_transport_key(), base + "/v1/messages", True
    return os.environ.get("ANTHROPIC_API_KEY", ""), _ANTHROPIC_API_URL, False


_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Dated claude-* IDs only exist on Anthropic's API; a LiteLLM-style proxy serves
# tier aliases instead. Only applied when proxied.
_PROXY_MODEL_MAP = {
    "claude-haiku-4-5-20251001": "haiku",
    "claude-sonnet-4-6": "sonnet",
}

# Categorical confidence emitted by older/simpler producers, mapped onto the
# canonical numeric scale. Unknown strings raise — a misspelling must not
# silently read as a mid-confidence finding.
_LEGACY_CONFIDENCE = {"low": 0.25, "medium": 0.5, "high": 0.9}


def _normalise_confidence(value) -> float:
    """Accept canonical numeric confidence and legacy categorical producers.

    A malformed confidence from one producer must not drop the rest of its
    findings — warn and default to 0.5 rather than raising.
    """
    if isinstance(value, str) and value.lower() in _LEGACY_CONFIDENCE:
        return _LEGACY_CONFIDENCE[value.lower()]
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        logger.warning(f"Unparseable confidence {value!r}; defaulting to 0.5")
        return 0.5
    if not 0.0 <= confidence <= 1.0:
        logger.warning(f"Out-of-range confidence {confidence}; defaulting to 0.5")
        return 0.5
    return confidence


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so authorization headers never cross origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen_https(req: urllib.request.Request, *, timeout: int, host: str):
    """Open one trusted HTTPS origin without following redirects."""
    parsed = urllib.parse.urlparse(req.full_url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ValueError("refusing non-HTTPS or unexpected request host")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(req, timeout=timeout)  # nosec B310


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
        confidence: float | str = 0.8,
        finding_id: Optional[str] = None,
    ):
        self.source = source
        self.file = file
        self.line = line
        self.severity = severity
        self.message = message
        self.fix = fix
        self.confidence = _normalise_confidence(confidence)
        # Synthesize a stable ID when the producer did not supply one.
        identity = f"{source}|{file}|{line}|{message}"
        self.finding_id = finding_id or hashlib.sha256(identity.encode()).hexdigest()[:12]

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
        self._has_first_party_candidates = False
        self._skipped_first_party: list[str] = []
        self._candidate_load_errors = 0
        self._skipped_other: list[str] = []
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

        if not self.candidates_dir.is_dir():
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
                if data.get("first_party") and candidate_findings:
                    # Only actual first-party findings force the direct key:
                    # a `skipped` marker carries nothing restricted, so it
                    # belongs on the advisory list (disclosed missing coverage)
                    # rather than vetoing adjudication of proxy-eligible
                    # findings from the rest of the batch.
                    self._has_first_party_candidates = True
                if data.get("first_party") and data.get("skipped"):
                    self._skipped_first_party.append(source)
                elif data.get("skipped"):
                    # Non-first-party skip (e.g. broad agent with no proxy
                    # credential) — not fail-closed, but still a coverage gap
                    # the review must disclose.
                    self._skipped_other.append(source)

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
                self._candidate_load_errors += 1

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
four filters: Accuracy (is the claim verifiable?), Actionability (is there a concrete fix?),
Novelty (is this a duplicate or already caught by CI?), and Specificity (does the finding
identify an exact location and concrete failure mechanism?).

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

Apply all four gates strictly. Only include findings in "surviving" that pass all gates.
If any ERROR survives, set review_action to "REQUEST_CHANGES"; otherwise "COMMENT".
"""

    def invoke_judge_agent(self, findings: list[Finding]) -> Optional[dict]:
        """
        Invoke the judge agent via the Messages API (Anthropic direct or
        LiteLLM proxy, per _endpoint()).
        Returns parsed judge response or None on failure.
        """
        # A candidate set carrying first_party findings must be adjudicated
        # on a first-party model — the OSS proxy would let an OSS backend drop
        # a true security finding before it is ever posted. With no direct
        # credential configured the judge fails closed rather than adjudicating
        # that set on the wrong plane.
        # CLIENT_AI_POLICY engagements keep the adjudication itself direct —
        # candidate findings carry PR content regardless of first_party flags.
        first_party = self._has_first_party_candidates or bool(os.environ.get("CLIENT_AI_POLICY"))
        api_key, api_url, proxied = _endpoint(direct=first_party)
        if first_party and not api_key:
            logger.error(
                "first-party findings present but no direct Anthropic "
                "credential (ANTHROPIC_DIRECT_API_KEY) — refusing to "
                "adjudicate security candidates via the OSS proxy"
            )
            return None
        if not api_key:
            logger.error("no API credential set; cannot invoke judge agent")
            return None

        # Build the user message with findings in untrusted_candidates tags
        findings_json = json.dumps(
            [f.to_dict() for f in findings],
            indent=2,
        )
        user_message = f"""Apply the 4-gate filter to these findings and return your decision.

<untrusted_candidates>
{findings_json}
</untrusted_candidates>

Remember: pass all four gates or drop the finding. Return only valid JSON, no markdown fences."""

        # Call the Messages API (direct Anthropic or governed proxy).
        try:
            model = "claude-sonnet-4-6"
            if proxied:
                model = _PROXY_MODEL_MAP.get(model, model)
            request_body = {
                "model": model,
                "max_tokens": 4000,
                "system": self.get_judge_system_prompt(),
                "messages": [
                    {"role": "user", "content": user_message}
                ],
            }

            headers = {
                "anthropic-version": _ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            }
            if proxied:
                headers["Authorization"] = f"Bearer {api_key.removeprefix('Bearer ')}"
            else:
                headers["x-api-key"] = api_key
            req = urllib.request.Request(
                api_url,
                data=json.dumps(request_body).encode("utf-8"),
                headers=headers,
                method="POST",
            )

            logger.info("Invoking judge agent...")
            with _urlopen_https(
                req,
                timeout=120,
                host=urllib.parse.urlparse(api_url).hostname or "",
            ) as response:
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

        # OSError, not urllib.error.URLError — urlopen converts only
        # request-phase failures; getresponse() failures (RemoteDisconnected,
        # ConnectionResetError, bare TimeoutError) propagate raw and would
        # kill the judge instead of taking the advisory-warning path.
        except (OSError, ValueError) as e:
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
        # The key also carries the effective action and candidate-load state — a
        # prior same-commit APPROVE (or a full-coverage verdict) must not
        # suppress a rerun whose coverage regressed, or the stale approval stays
        # live on partial evidence (Codex P1 + CodeRabbit major on
        # ai-starter-pack#150).
        findings_kind = f"findings-{review_action.lower()}"
        if self._candidate_load_errors:
            findings_kind += "-partial"
        if self._review_exists_at_sha(findings_kind):
            logger.info(f"Review already posted at {self.sha[:8]}; skipping")
            return True

        # Format review body
        body_lines = [
            REVIEW_MARKER,
            f"<!-- meta:{self.meta_digest}:{findings_kind} -->",
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
        skipped_all = sorted(set(self._skipped_first_party) | set(self._skipped_other))
        if skipped_all:
            # Findings survived the gates but some checks never ran — the
            # review must disclose the coverage gap, not read as a complete
            # assessment.
            skipped = ", ".join(_markdown_safe(s) for s in skipped_all)
            body_lines.append(
                f"**Incomplete coverage:** some review checks did not "
                f"run (skipped: {skipped})."
            )
            body_lines.append("")
        if self._candidate_load_errors:
            # Partially-corrupt batch with surviving findings still needs the
            # disclosure — otherwise an APPROVE reads as complete coverage
            # (upstream CodeRabbit finding on ai-starter-pack#150).
            body_lines.append(
                f"**Incomplete coverage:** {self._candidate_load_errors} "
                "candidate artifact(s) failed to load; surviving findings "
                "may understate the assessment."
            )
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
            with _urlopen_https(req, timeout=30, host="api.github.com") as response:
                result = json.loads(response.read().decode("utf-8"))
                review_id = result.get("id", "unknown")
                logger.info(f"Review posted successfully (ID: {review_id})")
                return True

        # OSError: same response-phase rationale as the API call above — a
        # proxy that accepts then drops the connection raises raw OSError.
        except OSError as e:
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

                with _urlopen_https(req, timeout=10, host="api.github.com") as response:
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
                    # A DISMISSED verdict is dead: judged→none→judged at the
                    # same SHA would otherwise find the dismissed review and
                    # suppress the replacement assessment.
                    and review.get("state") != "DISMISSED"
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

    def _dismiss_stale_judge_reviews(self) -> None:
        """Dismiss prior judge reviews at this SHA left in a blocking state.

        Only CHANGES_REQUESTED is actionable — GitHub 422s on COMMENTED, and a
        clean reassessment does not contradict an APPROVED. Best-effort: a
        dismissal failure logs and never blocks the clean comment.
        """
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
                with _urlopen_https(req, timeout=10, host="api.github.com") as response:
                    reviews = json.loads(response.read().decode("utf-8"))
            except Exception as e:
                logger.error(f"Failed to scan reviews for dismissal (page {page}): {e}")
                return

            if not isinstance(reviews, list):
                logger.error("Unexpected reviews response shape — skipping dismissals")
                return

            for review in reviews:
                author = (review.get("user") or {}).get("login")
                body = review.get("body") or ""
                if (
                    review.get("commit_id") == self.sha
                    and author == JUDGE_REVIEW_AUTHOR
                    and REVIEW_MARKER in body
                    and review.get("state") == "CHANGES_REQUESTED"
                    and review.get("id")
                ):
                    self._dismiss_review(review["id"])

            if len(reviews) < _REVIEWS_PER_PAGE:
                return

    def _dismiss_review(self, review_id: int) -> None:
        url = (
            f"https://api.github.com/repos/{self.repo}/pulls/"
            f"{self.pr_number}/reviews/{review_id}/dismissals"
        )
        req = urllib.request.Request(
            url,
            data=json.dumps(
                {"message": "Superseded by a newer reassessment at the same commit."}
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="PUT",  # dismissals endpoint is PUT-only; POST is rejected
        )
        try:
            with _urlopen_https(req, timeout=10, host="api.github.com"):
                logger.info(f"Dismissed stale judge review {review_id}")
        except Exception as e:
            logger.warning(f"Dismissal of review {review_id} failed ({e}); continuing")

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
            if not self.candidates_dir.is_dir():
                # Missing artifacts ≠ verified-empty: the assessment pipeline
                # never ran, so a clean verdict here could satisfy auto-merge on
                # zero evidence (upstream review finding: Codex P1). A path
                # that exists but is not a directory is the same case — its
                # *.json glob yields zero artifacts with no load error.
                logger.warning(
                    f"Candidates directory missing: {self.candidates_dir} — "
                    "posting advisory, not a clean verdict"
                )
                self._post_advisory_warning(
                    "Assessment artifacts unavailable (.ai/candidates missing) — "
                    "no specialist review evidence; clean verdict withheld."
                )
                return 0
            logger.info("No findings to process; posting verdict comment")
            self._post_clean_or_skip_advisory()
            return 0

        # Apply specificity gate (programmatic backstop)
        findings = self.apply_specificity_gate(findings)
        logger.info(f"{len(findings)} findings remain after specificity gate")

        if not findings:
            logger.info("All findings dropped by specificity gate")
            self._post_clean_or_skip_advisory()
            return 0

        # Invoke judge agent for 4-gate filter
        judge_result = self.invoke_judge_agent(findings)

        if not judge_result:
            logger.error("Judge agent invocation failed")
            self._post_advisory_warning(self._skipped_coverage_detail())
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
        if review_action == "APPROVE" and self._candidate_load_errors:
            # An APPROVE would satisfy the auto-merge verdict on partial
            # coverage — the unparseable candidates may have carried the
            # blocking finding. Force COMMENT; REQUEST_CHANGES is already the
            # stricter path (CodeRabbit major on ai-starter-pack#150).
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
                    "Passed all 4 gates",
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
            elif review_action != "REQUEST_CHANGES":
                # A same-SHA rerun whose verdict flipped CHANGES_REQUESTED →
                # nonblocking must retire the stale block: judge_fallback
                # honours ANY matching CHANGES_REQUESTED on the SHA (Codex P2
                # on buromaster#261). Best-effort — failures only log.
                self._dismiss_stale_judge_reviews()
        else:
            logger.info("No findings survived filters; skipping GitHub post")
            self._post_clean_or_skip_advisory()

        return 0

    def _post_no_findings_comment(self) -> None:
        """Post a COMMENT noting no findings (idempotent — skips if already posted)."""
        if not self.token or not self.repo:
            logger.info("No findings; skipping GitHub post (no token/repo)")
            return

        if self._review_exists_at_sha("clean"):
            logger.info("No-findings comment already posted for this SHA — skipping")
            # The clean verdict is confirmed present — safe to retire any stale
            # blocking review left over from a same-commit findings run.
            self._dismiss_stale_judge_reviews()
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

            with _urlopen_https(req, timeout=30, host="api.github.com"):
                logger.info("No-findings comment posted")

            # A same-commit rerun that reports clean does not supersede an
            # earlier judge REQUEST_CHANGES — a COMMENT sits alongside it and
            # the PR keeps the blocking verdict. Dismiss it only after the
            # replacement clean review is confirmed posted, so a failed POST
            # never leaves the PR with no verdict at all.
            self._dismiss_stale_judge_reviews()

        except Exception as e:
            logger.warning(f"Failed to post no-findings comment: {e}")

    def _skipped_coverage_detail(self) -> str:
        parts: list[str] = []
        if self._skipped_first_party:
            sources = ", ".join(sorted(set(self._skipped_first_party)))
            parts.append(f"First-party security checks did not run (skipped: {sources})")
        if self._skipped_other:
            sources = ", ".join(sorted(set(self._skipped_other)))
            parts.append(f"Other review checks did not run (skipped: {sources})")
        return ". ".join(parts) + ("." if parts else "")

    def _post_clean_or_skip_advisory(self) -> None:
        """Post the clean verdict — unless a first-party specialist was skipped.

        A skipped marker means a security specialist never ran, so 'no
        findings' is not a real verdict: fail closed into the advisory.
        """
        if self._candidate_load_errors:
            # Unparseable artifacts ≠ clean: a partially-corrupt batch (one bad
            # file + all surviving findings later dropped) must not satisfy the
            # auto-merge verdict either (upstream Codex P1 follow-up).
            logger.warning(
                f"{self._candidate_load_errors} candidate file(s) failed to "
                "parse — posting advisory, not a clean verdict"
            )
            self._post_advisory_warning(
                "Assessment artifacts unparseable (.ai/candidates) — "
                "incomplete specialist review evidence; clean verdict "
                "withheld."
            )
            return
        if self._skipped_first_party or self._skipped_other:
            sources = ", ".join(
                sorted(set(self._skipped_first_party) | set(self._skipped_other))
            )
            logger.warning(f"review checks skipped (missing credential): {sources}")
            self._post_advisory_warning(self._skipped_coverage_detail())
            return
        self._post_no_findings_comment()

    def _post_advisory_warning(self, detail: str = "") -> None:
        """Post an advisory WARNING when judge fails or coverage is incomplete."""
        if not self.token or not self.repo:
            logger.info("Judge failed; no token/repo for advisory post")
            return

        try:
            request_body = {
                "body": (
                    f"{REVIEW_MARKER}\n"
                    "## PR Assessment\n"
                    f"⚠️ Assessment system encountered an error.{f' {detail}' if detail else ''} "
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

            with _urlopen_https(req, timeout=30, host="api.github.com"):
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
        help="PR head SHA for duplicate-review detection — GITHUB_SHA on a pull_request event is the synthetic merge commit, while reviews bind to the head SHA (defaults to GITHUB_SHA env; required)",
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
