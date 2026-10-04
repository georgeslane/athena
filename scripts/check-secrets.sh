#!/usr/bin/env bash
# Check what's about to leave this machine for secrets and personal data. The git hooks
# from scripts/install-git-hooks.sh run it before every commit and push, and CI runs it
# on everything that reaches GitHub:
#
#   bash scripts/check-secrets.sh pre-commit   # the staged changes
#   bash scripts/check-secrets.sh pre-push     # the commits a push sends (reads git's hook input)
#   bash scripts/check-secrets.sh history      # every commit on every branch
#
# It stops:
#   - secrets, found by gitleaks (brew install gitleaks) with the rules in .gitleaks.toml
#   - files that .gitignore keeps out because they hold secrets or personal data (.env,
#     config.toml, data/, ...) but were added anyway with `git add -f`
#   - anything listed in .personal-blocklist: personal details only you know to look for,
#     like your Telegram user ID or address. That file is git-ignored, so it stays here.
#
# Written for bash 3.2, which is what macOS has.
set -euo pipefail

BLOCKLIST=.personal-blocklist
ZERO_OID='^0+$'
problems=0

fail() {
  printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2
  problems=1
}

# Lines a diff adds, without its file headers.
added_lines() {
  grep '^+' | grep -Ev '^\+\+\+ (b/|/dev/null)' || true
}

# stdin: NUL-separated paths. Reports those that .gitignore keeps out of git.
check_ignored() {
  local path paths=() found=()
  while IFS= read -r -d '' path; do
    [[ -n "$path" ]] && paths+=("$path")
  done
  ((${#paths[@]})) || return 0
  # Only this repo's .gitignore counts, not your personal one.
  while IFS= read -r -d '' path; do
    found+=("$path")
  done < <(printf '%s\0' "${paths[@]}" | git -c core.excludesFile=/dev/null check-ignore --no-index -z --stdin || true)
  if ((${#found[@]})); then
    fail "These are git-ignored because they can hold secrets or personal data, but were added anyway:"
    printf '    %s\n' "${found[@]}" >&2
  fi
}

# stdin: everything that would leave this machine. Reports lines of the blocklist found in it.
check_blocklist() {
  if [[ ! -f "$BLOCKLIST" ]]; then
    cat >/dev/null
    return
  fi
  local text entry n=0
  text="$(cat)"
  while IFS= read -r entry || [[ -n "$entry" ]]; do
    n=$((n + 1))
    entry="${entry#"${entry%%[![:space:]]*}"}" # trim
    entry="${entry%"${entry##*[![:space:]]}"}"
    [[ -z "$entry" || "$entry" == \#* ]] && continue
    # Says which line matched rather than what it is, so the detail isn't printed.
    if grep -qiF -- "$entry" <<<"$text"; then
      fail "Line $n of $BLOCKLIST appears in what you're about to send."
    fi
  done <"$BLOCKLIST"
}

check_secrets() {
  if ! command -v gitleaks >/dev/null 2>&1; then
    fail "gitleaks isn't installed, so this can't be checked for secrets. Install it: brew install gitleaks"
    return
  fi
  gitleaks git --config .gitleaks.toml --no-banner --redact --verbose --log-level warn "$@" . >&2 ||
    fail "gitleaks found what looks like a secret (above)."
}

# Arguments: git log revisions, like `A..B` or `--all`.
check_commits() {
  check_ignored < <(git log -z --name-only --diff-filter=ACR --format= "$@")
  check_blocklist < <(
    git log --format=%B "$@" # commit messages
    git log -p --no-color --no-ext-diff -U0 --format= "$@" | added_lines
  )
  check_secrets --log-opts="$*"
}

cd "$(git rev-parse --show-toplevel)"

case "${1:-}" in
  pre-commit)
    check_ignored < <(git diff --cached -z --name-only --diff-filter=ACR)
    check_blocklist < <(git diff --cached --no-color --no-ext-diff -U0 | added_lines)
    check_secrets --staged
    ;;
  pre-push)
    # Git sends one line per branch: <local ref> <local oid> <remote ref> <remote oid>.
    ranges=()
    while read -r _ local_oid _ remote_oid; do
      [[ "$local_oid" =~ $ZERO_OID ]] && continue # deleting a branch sends nothing
      if [[ "$remote_oid" =~ $ZERO_OID ]] || ! git cat-file -e "$remote_oid^{commit}" 2>/dev/null; then
        ranges+=("$local_oid --not --remotes") # a new branch: everything that isn't on GitHub yet
      else
        ranges+=("$remote_oid..$local_oid")
      fi
    done
    for range in ${ranges[@]+"${ranges[@]}"}; do
      # Each range is several git arguments, so it's split on purpose.
      # shellcheck disable=SC2086
      if [[ -n "$(git rev-list $range)" ]]; then
        check_commits $range
      fi
    done
    ;;
  history)
    check_commits --all
    ;;
  *)
    echo "Usage: bash scripts/check-secrets.sh pre-commit|pre-push|history" >&2
    exit 2
    ;;
esac

if ((problems)); then
  if [[ "$1" == history ]]; then
    echo "If that's a real secret, it may already be public: revoke it and make a new one." >&2
  else
    echo "Nothing was sent. Remove the above and try again. For a false alarm from gitleaks, add" >&2
    echo "the comment 'gitleaks:allow' to that line; to skip these checks once, use --no-verify." >&2
  fi
  exit 1
fi
printf '\033[1;32m✓ No secrets or personal data found.\033[0m\n' >&2
