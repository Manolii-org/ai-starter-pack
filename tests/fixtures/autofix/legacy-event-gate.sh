# Historical negative fixture. Never used by runtime workflows.
set -euo pipefail
echo "number=$PR_NUM" >> "$GITHUB_OUTPUT"
echo "trigger_workflow=$WR_NAME" >> "$GITHUB_OUTPUT"
echo "trigger_run_url=$WR_URL" >> "$GITHUB_OUTPUT"

# Loop break: prior autofix commit still HEAD. Preflight has no
# checkout — read the head commit message via the API instead of
# `git log` (same assertion: subject starts with [autofix]).
PR_HEAD=$(timeout 20 gh api "repos/${REPOSITORY}/pulls/${PR_NUM}" \
  --jq '. as $pr | $pr.head | {sha: .sha, same_repo: (if .repo == null then "missing" elif (.repo.id == $pr.base.repo.id) then "true" else "false" end)}' \
  2>/dev/null || echo '{}')
HEAD_SHA=$(echo "$PR_HEAD" | jq -r '.sha // empty')
SAME_REPO=$(echo "$PR_HEAD" | jq -r '.same_repo // empty')
echo "head_sha=$HEAD_SHA" >> "$GITHUB_OUTPUT"
if [[ -z "$HEAD_SHA" ]]; then
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
  LAST_MSG=$(timeout 20 gh api "repos/${REPOSITORY}/commits/${HEAD_SHA}" \
    --jq '.commit.message | split("\n")[0]' 2>/dev/null || echo '')
fi
if [[ "$LAST_MSG" == \[autofix\]* ]]; then
  echo "skip=true" >> "$GITHUB_OUTPUT"
  echo "Latest commit is [autofix]; skipping to avoid loop"
  exit 0
fi

if echo "$COMMENT_BODY" | grep -qi "skip-autofix"; then
  echo "skip=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Human issue_comment needs @claude; relayed **[@ and workflow_run do not
if [[ "$EVENT_NAME" == "issue_comment" ]]; then
  if [[ "$ACTOR" == "github-actions[bot]" ]] && [[ "$COMMENT_BODY" == '**[@'* ]]; then
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
# Agent bots reach the action only on the CI-failure path; comment
# and review triggers keep the relay bot alone.
allowed_bots="github-actions"
if [[ "$EVENT_NAME" == "workflow_run" ]]; then
  for entry in $(trusted_bots); do allowed_bots+=",$entry"; done
fi
echo "allowed_bots=$allowed_bots" >> "$GITHUB_OUTPUT"

# claude-code-action fails the job (rather than skipping) for bot
# actors outside allowed_bots and for actors without write access.
# Only the relay bot and opted-in agent bots are allowed; skip
# everything else before dispatch.
for who in "$ACTOR" "${WR_ACTOR:-}"; do
  [[ -z "$who" ]] && continue
  if [[ "$who" == "github-actions[bot]" && "$EVENT_NAME" == "issue_comment" && "$COMMENT_BODY" == '**[@'* ]]; then
    continue
  fi
  if [[ "$EVENT_NAME" == "workflow_run" && "$who" == *"[bot]" ]] && trusted_bot "$who"; then
    continue
  fi
  PERM=$(timeout -k 5 30 gh api "repos/${REPOSITORY}/collaborators/${who}/permission" --jq '.permission' 2>/dev/null || echo unknown)
  case "$PERM" in
    admin|write) ;;
    *)
      echo "skip=true" >> "$GITHUB_OUTPUT"
      echo "Actor ${who} has no write access or is a non-relay bot (permission=${PERM}); skipping"
      exit 0
      ;;
  esac
done

# --repo required: this step runs with no checkout, so gh would
# resolve the repo from cwd and fail. Failure must fail closed —
# a label list we can't read might contain skip-ai-review.
LABELS=$(timeout 20 gh pr view "$PR_NUM" --repo "${REPOSITORY}" --json labels --jq '[.labels[].name]' 2>/dev/null || echo 'FAIL')
if [[ "$LABELS" == "FAIL" ]] || echo "$LABELS" | grep -q "skip-ai-review"; then
  echo "skip=true" >> "$GITHUB_OUTPUT"
else
  echo "skip=false" >> "$GITHUB_OUTPUT"
fi
