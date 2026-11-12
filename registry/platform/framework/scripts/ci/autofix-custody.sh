#!/usr/bin/env bash
# Embedded into the trusted workflow by sync-autofix-control.py. Never source a PR copy.
set -euo pipefail
autofix_custody() {
  local pr author page comments count
  [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ && "${EXPECTED_HEAD:-}" =~ ^[0-9a-f]{40}$ ]] || return 1
  if ! pr=$(timeout -k 5 30 gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null); then
    echo 'Ownership readback unavailable; no editor or publication admitted.'
    return 1
  fi
  if ! jq -e --arg repo "$REPOSITORY" --arg head "$EXPECTED_HEAD" '
    .state == "open" and .head.sha == $head and
    (.head.repo.full_name | ascii_downcase) == ($repo | ascii_downcase) and
    (.user.id | type) == "number" and
    ([.labels[].name] | any(. == "skip-ai-review" or . == "human-merge" or . == "security-remediation") | not)
  ' <<< "$pr" >/dev/null; then
    echo 'Native PR state, head, fork or human hold prevents admission.'
    return 1
  fi
  if jq -e '(.body // "") | gsub("(?s)```.*?```"; "") | test("(?m)^X-Follow-Through-Owner:")' <<< "$pr" >/dev/null; then
    echo 'Existing session owner retained. No editor or publication admitted; GitHub facts remain with that owner.'
    return 1
  fi
  author=$(jq -r '.user.id' <<< "$pr")
  for page in 1 2 3 4 5; do
    if ! comments=$(timeout -k 5 30 gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments?per_page=100&page=${page}" 2>/dev/null) ||
       ! jq -e 'type == "array"' <<< "$comments" >/dev/null; then
      echo 'Owner registration readback incomplete; admission held.'
      return 1
    fi
    if jq -e --argjson author "$author" 'any(.[];
      (.user.id == $author or .user.id == 41898282) and
      ((.body // "") | startswith("<!-- follow-through-owner:v1 -->")))' <<< "$comments" >/dev/null; then
      echo 'Existing session owner retained by its registration. No editor or publication admitted.'
      return 1
    fi
    if jq -e --arg head "$EXPECTED_HEAD" 'any(.[];
      .user.id == 41898282 and ((.body // "") | test(
        "^<!-- pack-autofix-publication:v1 head=" + $head +
        " outcome=(protected_head_held|head_protection_unverified|publication_packet_held|publication_admission_held|native_state_held|instruction_surface_held|publication_credential_unavailable|publication_unknown|native_state_unverified|commit_subject_held|candidate_ancestry_held) -->")))' <<< "$comments" >/dev/null; then
      echo 'This head has held or uncertain publication. Its existing owner must recover retained work and resolve that dependency; new GitHub facts remain pending. No duplicate editor admitted.'
      return 1
    fi
    count=$(jq 'length' <<< "$comments")
    [[ "$count" -lt 100 ]] && return 0
  done
  echo 'Owner registration pagination bound reached; admission held.'
  return 1
}
if autofix_custody; then
  echo 'skip=false' >> "$GITHUB_OUTPUT"
else
  echo 'skip=true' >> "$GITHUB_OUTPUT"
fi
