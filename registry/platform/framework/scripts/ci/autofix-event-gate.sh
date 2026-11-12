#!/usr/bin/env bash
# Canonical native PR/event admission; embedded, never sourced from PR code.
set -euo pipefail

echo "number=$PR_NUM" >> "$GITHUB_OUTPUT"
echo "trigger_workflow=$WR_NAME" >> "$GITHUB_OUTPUT"
echo "trigger_run_url=$WR_URL" >> "$GITHUB_OUTPUT"

# Loop break: prior autofix commit still HEAD. Preflight has no
# checkout — read the head commit message via the API instead of
# `git log` (same assertion: subject starts with [autofix]).
PR_HEAD=$(timeout 20 gh api "repos/${REPOSITORY}/pulls/${PR_NUM}" \
  --jq '. as $pr | $pr.head | {sha: .sha, opener: ($pr.user.login // ""), same_repo: (if .repo == null then "missing" elif (.repo.id == $pr.base.repo.id) then "true" else "false" end)}' \
  2>/dev/null || echo '{}')
HEAD_SHA=$(echo "$PR_HEAD" | jq -r '.sha // empty')
SAME_REPO=$(echo "$PR_HEAD" | jq -r '.same_repo // empty')
echo "head_sha=$HEAD_SHA" >> "$GITHUB_OUTPUT"
if [[ ! "$HEAD_SHA" =~ ^[0-9a-f]{40}$ || ( -n "${ADMITTED_REVIEW_HEAD:-}" && "$HEAD_SHA" != "$ADMITTED_REVIEW_HEAD" ) ]]; then
  # Fail closed: an empty SHA would fall back to movable head_ref in
  # the autofix checkout — unvalidated commit. Skip instead.
  echo "skip=true" >> "$GITHUB_OUTPUT"
  echo "Could not resolve PR head SHA; skipping"
  exit 0
fi
if [[ "$SAME_REPO" != "true" ]]; then
  # Compare repo IDs, not .fork (pr-autofix-loop-reusable.yml): a
  # forked deployment repo has fork=true on its own PRs, and a
  # deleted head has repo=null — both must fail closed. A cross-repo
  # head is attacker-controlled code under write perms + agent creds
  # on issue_comment/pull_request triggers.
  echo "skip=true" >> "$GITHUB_OUTPUT"
  echo "Cross-repo/unverifiable PR head (same_repo=$SAME_REPO) — refusing privileged autofix checkout"
  exit 0
fi
LAST_MSG=''
if [[ -n "$HEAD_SHA" ]]; then
  if ! LAST_MSG=$(timeout 20 gh api "repos/${REPOSITORY}/commits/${HEAD_SHA}" \
    --jq '.commit.message | split("\n")[0]' 2>/dev/null); then
    echo 'skip=true' >> "$GITHUB_OUTPUT"
    echo 'Head commit metadata is unreadable; editor admission held.'
    exit 0
  fi
fi
if [[ "$LAST_MSG" == \[autofix\]* ]]; then
  echo "skip=true" >> "$GITHUB_OUTPUT"
  echo "Latest commit is [autofix]; skipping to avoid loop"
  exit 0
fi

# Native webhook identity, never relayed body text, grants reviewer admission.
# Embedded in protected workflow source. All review content remains untrusted.
review_admitted=false
case "$EVENT_NAME" in
  issue_comment|pull_request_review|pull_request_review_comment)
    if [[ "${ACTOR_TYPE:-}" == Bot ]]; then
      if ! jq -e 'type == "array" and all(.[]; type == "string" and test("^[1-9][0-9]*$"))' <<< "$REVIEWER_IDS" >/dev/null 2>&1; then
        echo 'skip=true' >> "$GITHUB_OUTPUT"
        echo 'Approved reviewer ID configuration is invalid; editor admission held.'
        exit 0
      fi
      if jq -e --arg id "${ACTOR_ID:-}" 'index($id) != null' <<< "$REVIEWER_IDS" >/dev/null; then
        # Match the job-level inexpensive cue filter. Mixed findings/limit
        # banners remain eligible; an unchanged healthy notice is not progress.
        if [[ -z "${COMMENT_BODY//[[:space:]]/}" ]] ||
           grep -qi 'auto-generated reply by CodeRabbit' <<< "$COMMENT_BODY" ||
           { ! grep -Eqi "$REVIEW_FINDING_PATTERN" <<< "$COMMENT_BODY" &&
             grep -Eqi "$REVIEW_INFORMATIONAL_PATTERN" <<< "$COMMENT_BODY"; }; then
          echo 'skip=true' >> "$GITHUB_OUTPUT"
          echo 'Informational reviewer event; no editor admitted.'
          exit 0
        fi
        if [[ -n "${REVIEW_SOURCE_HEAD:-}" ]]; then
          if [[ ! "$REVIEW_SOURCE_HEAD" =~ ^[0-9a-f]{40}$ || "$HEAD_SHA" != "$REVIEW_SOURCE_HEAD" ||
                ( -n "${ADMITTED_REVIEW_HEAD:-}" && "$ADMITTED_REVIEW_HEAD" != "$REVIEW_SOURCE_HEAD" ) ]]; then
            echo 'skip=true' >> "$GITHUB_OUTPUT"
            echo 'Review head is stale or unreadable; original GitHub facts remain pending.'
            exit 0
          fi
        fi
        review_admitted=true
      fi
    fi
    ;;
esac

if [[ "$review_admitted" != true ]] && echo "$COMMENT_BODY" | grep -qi "skip-autofix"; then
  echo "skip=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Human issue_comment needs @claude. Verified native reviews, legacy relay
# compatibility events and CI failures use their own admission routes.
if [[ "$EVENT_NAME" == "issue_comment" ]]; then
  if [[ "$review_admitted" == true ]] || { [[ "$ACTOR" == "github-actions[bot]" ]] && [[ "$COMMENT_BODY" == '**[@'* ]]; }; then
    : # relayed bot review — proceed
  elif ! echo "$COMMENT_BODY" | grep -qi "@claude"; then
    echo "skip=true" >> "$GITHUB_OUTPUT"
    exit 0
  fi
fi

if [[ "$EVENT_NAME" == "pull_request_review" ]]; then
  if [[ "$REVIEW_STATE" == "approved" ]] && [[ -z "$COMMENT_BODY" || "$COMMENT_BODY" == "null" ]]; then
    echo "skip=true" >> "$GITHUB_OUTPUT"
    exit 0
  fi
fi

# Opt-in trusted agent bots (e.g. Devin, Codex cloud, Cursor
# background agents) whose pushes may trigger Autofix when CI fails.
# Only on the workflow_run path, only for [bot] actors, matched like
# the action's allowed_bots (case-insensitive, [bot] optional); '*'
# is never honoured. Fork heads were already refused above.
# The action's allowed_bots receives only this sanitised list, so a
# '*' or malformed entry can never widen it either.
trusted_bots() {
  local entry
  IFS=',' read -ra entries <<< "${TRUSTED_BOTS:-}"
  for entry in "${entries[@]}"; do
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry%"${entry##*[![:space:]]}"}"
    entry="${entry,,}"
    entry="${entry%\[bot\]}"
    if [[ "$entry" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then echo "$entry"; fi
  done
}
trusted_bot() {
  local who="${1,,}" entry
  who="${who%\[bot\]}"
  for entry in $(trusted_bots); do
    [[ "$entry" == "$who" ]] && return 0
  done
  return 1
}
# CI push bots remain opt-in. Authenticated direct reviewer events use
# their own immutable-ID policy; they do not widen the CI actor policy.
allowed_bots="github-actions"
if [[ "$review_admitted" == true ]]; then
  reviewer_login="${ACTOR,,}"
  if [[ ! "$reviewer_login" =~ ^[a-z0-9][a-z0-9-]*(\[bot\])?$ ]]; then echo "skip=true" >> "$GITHUB_OUTPUT"; exit 0; fi
  allowed_bots+=",${reviewer_login%\[bot\]}"
fi
if [[ "$EVENT_NAME" == "workflow_run" ]]; then
  for entry in $(trusted_bots); do allowed_bots+=",$entry"; done
fi
echo "allowed_bots=$allowed_bots" >> "$GITHUB_OUTPUT"

# claude-code-action fails the job (rather than skipping) for bot
# actors outside allowed_bots and for actors without write access.
# Native review admission, the relay compatibility route and opted-in CI
# bots are distinct. All other actors need native repository write access.
event_authorized=false
for who in "$ACTOR" "${WR_ACTOR:-}"; do
  [[ -z "$who" ]] && continue
  if [[ "$review_admitted" == true && "$who" == "$ACTOR" && "$EVENT_NAME" != workflow_run ]]; then continue; fi
  if [[ "$who" == "github-actions[bot]" && "$EVENT_NAME" == "issue_comment" && "$COMMENT_BODY" == '**[@'* ]]; then
    continue
  fi
  if [[ "$EVENT_NAME" == "workflow_run" && "$who" == *"[bot]" ]] && trusted_bot "$who"; then
    continue
  fi
  PERM=$(timeout -k 5 30 gh api "repos/${REPOSITORY}/collaborators/${who}/permission" --jq '.permission' 2>/dev/null || echo unknown)
  case "$PERM" in
    admin|write) event_authorized=true ;;
    *)
      echo "skip=true" >> "$GITHUB_OUTPUT"
      echo "Actor ${who} has no write access or is not an approved source (permission=${PERM}); skipping"
      exit 0
      ;;
  esac
done

# Bot identity authorizes delivery, not privileged execution for an arbitrary
# PR opener. When all event actors used bot waivers, require native write
# authority for the opener before any secret-bearing model action can run.
# An existing writable human actor still authorizes its normal event route.
if [[ "$event_authorized" != true ]]; then
  OPENER=$(jq -r '.opener // empty' <<< "$PR_HEAD")
  if [[ ! "$OPENER" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]*(\[bot\])?$ ]]; then
    echo 'skip=true' >> "$GITHUB_OUTPUT"
    echo 'Native PR opener is missing or invalid; bot-driven editor admission held.'
    exit 0
  fi
  OPENER_PERM=$(timeout -k 5 30 gh api "repos/${REPOSITORY}/collaborators/${OPENER}/permission" --jq '.permission' 2>/dev/null || echo unknown)
  case "$OPENER_PERM" in
    admin|write) ;;
    *)
      echo 'skip=true' >> "$GITHUB_OUTPUT"
      echo 'Native PR opener lacks verified write authority; bot-driven editor admission held. Its existing authorized owner must reconcile the PR/read route; no model or writer identity fallback.'
      exit 0
      ;;
  esac
fi

# --repo required: this step runs with no checkout, so gh would
# resolve the repo from cwd and fail. Failure must fail closed —
# a label list we can't read might contain skip-ai-review.
LABELS=$(timeout 20 gh pr view "$PR_NUM" --repo "${REPOSITORY}" --json labels --jq '[.labels[].name]' 2>/dev/null || echo 'FAIL')
if [[ "$LABELS" == "FAIL" ]] || echo "$LABELS" | grep -q "skip-ai-review"; then
  echo "skip=true" >> "$GITHUB_OUTPUT"
else
  echo "skip=false" >> "$GITHUB_OUTPUT"
fi
