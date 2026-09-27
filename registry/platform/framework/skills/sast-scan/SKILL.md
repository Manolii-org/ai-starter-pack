---
name: sast-scan
version: 1.0.0
description: "Run static application security testing (Semgrep, plus CodeQL when available) over changed files or a path, triage findings by exploitability, and promote true-positives to the security-deep-dive agent."
type: skill
data_sensitivity: internal
first_party: true  # eligibility matrix: security review stays on Anthropic even under OSS routing
safety_tier: green
requires_mcp: []
required_entities: []
allowed-tools:
  - Bash
  - Read
  - Grep
tags:
  - security
  - sast
  - review
intent_phrases:
  - "run a security scan"
  - "run SAST"
  - "check for vulnerabilities"
  - "scan for injection"
  - "check for secrets"
  - "semgrep"
  - "codeql"
disallowed-tools:
  - Edit
  - Write
  - NotebookEdit
---

# Skill: SAST Scan

Static application security testing (SAST) automation. Runs Semgrep and optionally CodeQL over specified paths or changed files, triages findings by exploitability likelihood, and surfaces true-positives for deeper security analysis.

## When to Use

- User asks to run a security scan, SAST, or check for vulnerabilities
- Scanning a diff before merge to catch injection/secrets/boundary issues
- Need to harden a codebase path with automated vulnerability detection
- **Read-only analysis only** — green tier, no mutations

## Workflow

### 1. Scope Selection
- If user specifies a path: use that directory
- If running on a PR branch: `git diff --name-only origin/main...HEAD` to get changed files
- If running on HEAD: scan the entire repo or a specified subdirectory

### 2. Run Semgrep
Verify Semgrep is installed — do **not** auto-install in this read-only workflow (env mutation breaks the green contract and fails on locked/offline runners):
```bash
command -v semgrep >/dev/null || { echo "semgrep not installed — ask the operator to install it (pip install semgrep); do not auto-install here"; exit 1; }
semgrep --config p/ci --config p/security-audit --config p/secrets <paths> --json > /tmp/semgrep.json
```
- `p/ci` = default Semgrep rules (OWASP Top 10, common injection, hardcoded secrets)
- `p/security-audit` = additional audit-focused rules
- `p/secrets` = dedicated secret detection (tokens, keys, DB URLs) — **required** for the "check for secrets" intent
- `--json` for structured output

### 3. Check CodeQL Results (If Available)
- CodeQL already runs in CI for Python/JavaScript repos
- If a GitHub Actions run exists on the same branch, fetch its SARIF artifact
- Do **not** re-run CodeQL locally unless explicitly requested — use CI results

### 4. Triage Each Finding
For every Semgrep finding:
- **File + line:** Where the issue occurs
- **Rule ID + severity:** What the rule flags
- **Data-flow reachability:** Is the flagged code path actually reachable from untrusted input?
- **Exploitability:** Assign true-positive (TP) likelihood on a 0–1 scale
  - ≥0.7 = promote to security-deep-dive agent
  - <0.7 = note as low-likelihood false positive with reasoning

### 5. Triage Heuristics (See reference.md)
Common rule classes and TP-likelihood signals:
- **Injection:** TP high if user input flows directly to query/command; low if strongly validated
- **Hardcoded secrets:** TP very high if AWS key / password pattern; medium if generic string
- **Path traversal:** TP high if `os.path.join(user_input, ...)` without normalization; low if constrained
- **SSRF:** TP high if user-controlled URL reaches `requests.get()` without allowlist
- **Deserialization:** TP high for pickle/yaml with user input; low for JSON

### 6. Summarise as Table
Output a markdown table with columns:
- `file:line` — location in code
- `rule` — Semgrep rule ID
- `severity` — CRITICAL / HIGH / MEDIUM / LOW (from Semgrep)
- `TP-likelihood` — 0.0–1.0 estimated true-positive probability
- `note` — brief data-flow or context note; flag if promoting to security-deep-dive

### 7. Promote True-Positives
For findings with TP-likelihood ≥0.7:
- Extract code excerpt (5–10 lines around the issue)
- Include file, line, rule ID, and data-flow rationale

**Multi-agent hand-off (required):** `security-deep-dive` runs only when `.ai/sast-findings.json` exists with ≥1 finding — it reads that file and skips entirely if absent (`.claude/agents/security-deep-dive.md`). To hand off you MUST write that file (via Bash — the Write tool is disallowed for this skill) as the **bare JSON array of raw findings** the agent's producer contract expects (see `.claude/agents/security-deep-dive.md` lines 49-60). Use Python stdlib (no `jq` dependency — it may be absent on minimal runners) and make `finding_id` unique per **file + line + rule** so identical rules on the same line in different files don't collide:
```bash
mkdir -p .ai
python3 - <<'PY'
import json, re
data = json.load(open("/tmp/semgrep.json"))
results = data.get("results", [])
# SECURITY: secret/credential hits must NOT be forwarded to the proxy-backed
# deep-dive (see prose below). Exclude them from the hand-off file.
SECRET_RE = re.compile(r"secret|credential|api[-_]?key|private[-_]?key|password|token", re.I)
def is_secret_finding(r):
    # A credential can surface under a generic rule id — classify on the
    # matched message/metadata, not just check_id.
    return SECRET_RE.search(r["check_id"]) or SECRET_RE.search(
        r.get("extra", {}).get("message", "")
    ) or SECRET_RE.search(r.get("extra", {}).get("metadata", {}).get("cwe", "") if isinstance(r.get("extra", {}).get("metadata"), dict) else "")
out = [{
    "finding_id": f"{r['path']}:{r['start']['line']}:{r['check_id']}",
    "rule": r["check_id"],
    "file": r["path"],
    "line": r["start"]["line"],
    "message": r.get("extra", {}).get("message", ""),
    "severity": r.get("extra", {}).get("severity", "MEDIUM"),
} for r in results if not is_secret_finding(r)]
json.dump(out, open(".ai/sast-findings.json", "w"), indent=2)
# Secret-rule hits go to the OPERATOR only — file:line + rule, never the value:
secret_hits = [f"{r['path']}:{r['start']['line']} {r['check_id']}" for r in results if is_secret_finding(r)]
if secret_hits:
    print("SECRET-RULE HITS (operator review only — NOT sent to deep-dive):\n" + "\n".join(secret_hits))
PY
```
Each element has `finding_id, rule, file, line, message, severity`. Write **all non-secret** Semgrep results here (not only your ≥0.7 promotions) — `security-deep-dive` performs the exploitability triage and filtering itself; your steps 4–6 triage drives the *standalone* human summary, not this hand-off file. Note this producer **input** is a bare array — distinct from the `{source, findings}` skill-**output** schema in `.claude/schemas/skill-findings.schema.json` (do not validate the hand-off file against that output schema). For the standalone path (no multi-agent), surface the triaged table for human review instead of writing the file.

> **SECURITY — secrets never reach the proxy deep-dive:** `security-deep-dive` is `model: sonnet`, which routes to the OSS proxy (Fireworks/DeepSeek), and `.claude/model-routing.json` forbids sending credentials/API keys there. The transform above therefore **excludes** secret/credential-rule hits (e.g. `p/secrets`) from `.ai/sast-findings.json` — it reads ±20 lines around each finding, which would expose the secret value. Surface secret hits to the operator directly (`file:line` + rule only, **never the value**) for rotation/removal — a detected hardcoded secret needs remediation, not exploitability triage.

## Safety & Scope

- **Green tier:** Read-only, no secrets in output, no code modifications
- **No credentials:** Semgrep runs offline (read-only, no environment mutations; must be pre-installed)
- **CI integration:** Respect existing CodeQL/SAST results; do not override without reason
- **Output only:** This skill reports findings; does not fix or suppress them

## Examples

Typical invocation:
```bash
# Scan changed files on a branch
/sast-scan --diff

# Scan a specific path
/sast-scan --path src/api

# Include CodeQL (if GitHub Actions integration available)
/sast-scan --diff --include-codeql
```

See `reference.md` for detailed rulesets, CodeQL integration, and triage guidance.
