#!/usr/bin/env bash
# Read-only audit: GitHub Actions repository variables are the sole relationship
# contract. Never embed or print concrete child repository identities.
set -euo pipefail

fail() {
  printf '::error::%s\n' "$1" >&2
  exit "${2:-2}"
}

[[ -n "${GH_TOKEN:-}" ]] || fail "The runtime API token is unavailable."
[[ -n "${GITHUB_REPOSITORY:-}" ]] || fail "The current parent repository is unknown."
command -v gh >/dev/null || fail "GitHub CLI is unavailable."
command -v jq >/dev/null || fail "jq is unavailable."

# Always read every variable page; any API error is an unavailable discovery,
# never a claim that a child is absent. Suppress error URLs containing names.
read_variables() {
  local repository="$1" result
  result="$(gh api --paginate "repos/$repository/actions/variables?per_page=100" 2>/dev/null |
    jq -cs '{variables: [.[].variables[]?]}')" ||
    fail "Repository-variable discovery is unavailable; refusing to guess." 4
  printf '%s' "$result"
}

variable_value() {
  jq -r --arg key "$2" '[.variables[]? | select(.name == $key) | .value][0] // ""' <<<"$1"
}

parent_vars="$(read_variables "$GITHUB_REPOSITORY")"
[[ "$(variable_value "$parent_vars" CONTINUUM_ROLE)" == "parent" ]] ||
  fail "This repository must declare CONTINUUM_ROLE=parent."

children="$(variable_value "$parent_vars" CONTINUUM_CHILDREN)"
jq -e '
  type == "array" and length > 0 and
  all(.[]; type == "string" and test("^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")) and
  (unique | length) == length
' <<<"$children" >/dev/null ||
  fail "CONTINUUM_CHILDREN must contain distinct opaque child IDs."

# Use the same provider-neutral discovery scope as Continuum's resolver.
# Enumerate candidates once (rather than once for every configured child).
candidates="$(gh api --paginate '/user/repos?affiliation=owner&per_page=100'   --jq '.[].full_name' 2>/dev/null)" ||
  fail "Repository discovery is unavailable; refusing to guess." 4

declare -A matched=()
while IFS= read -r repository; do
  [[ -n "$repository" && "$repository" != "$GITHUB_REPOSITORY" ]] || continue

  # Always mask before using a private repository identity in an API call.
  printf '::add-mask::%s\n' "$repository"
  candidate_vars="$(read_variables "$repository")"
  role="$(variable_value "$candidate_vars" CONTINUUM_ROLE)"
  child_id="$(variable_value "$candidate_vars" CONTINUUM_CHILD_ID)"
  parent="$(variable_value "$candidate_vars" CONTINUUM_PARENT)"

  # A candidate declaring this parent must be an allowed and valid child.
  if [[ "$parent" == "$GITHUB_REPOSITORY" ]]; then
    [[ -n "$child_id" ]] ||
      fail "A declared child has no child ID."
    jq -e --arg id "$child_id" 'index($id) != null' <<<"$children" >/dev/null ||
      fail "A declared child is not in the parent allow-list."
  fi

  # Detect a misbound allow-listed ID even if its parent is different.
  if [[ -n "$child_id" ]] &&
     jq -e --arg id "$child_id" 'index($id) != null' <<<"$children" >/dev/null; then
    [[ "$role" == "child" && "$parent" == "$GITHUB_REPOSITORY" ]] ||
      fail "An allowed child declares an inconsistent relationship."
    [[ -z "${matched[$child_id]:-}" ]] ||
      fail "More than one repository declares the same allowed child."
    matched["$child_id"]=1
  fi
done <<<"$candidates"

mapfile -t allowed_ids < <(jq -r '.[]' <<<"$children")
for child_id in "${allowed_ids[@]}"; do
  [[ "${matched[$child_id]:-}" == 1 ]] ||
    fail "At least one configured child cannot be verified uniquely."
done

printf 'Verified %s child binding(s) using GitHub Actions variables; no repository identities logged.\n' "${#allowed_ids[@]}"
