#!/usr/bin/env bash
# One material budget receipt; repeated exhausted-budget wakes stay quiet.
notice=unknown
for page in 1 2 3 4 5; do
  if ! comments=$(timeout -k 5 30 gh api "repos/${REPOSITORY}/issues/${PR_NUM}/comments?per_page=100&page=${page}" 2>/dev/null); then break; fi
  if ! jq -e 'type == "array"' <<< "$comments" >/dev/null; then break; fi
  if jq -e 'any(.[]; .user.id == 41898282 and ((.body // "") | startswith("<!-- pack-autofix:budget-exhausted -->")))' <<< "$comments" >/dev/null; then
    notice=seen; break
  fi
  if [[ $(jq 'length' <<< "$comments") -lt 100 ]]; then notice=absent; break; fi
done
if [[ "$notice" == absent ]]; then
  timeout -k 5 30 gh pr comment "$PR_NUM" --repo "$REPOSITORY" --body "<!-- pack-autofix:budget-exhausted --> Autofix reached its configured successful-fix budget (${COUNT}/${MAX_FIXES}). The original owner retains unfinished commitments. Do not remove commits, reset counters or switch owners to evade this budget." || true
fi
