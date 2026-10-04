#!/usr/bin/env bash
# Check every commit and push from this clone for secrets and personal data before it
# leaves this machine (see scripts/check-secrets.sh). Run it once on the computer you
# develop on, not on the Pi, which can't push:
#
#   brew install gitleaks
#   bash scripts/install-git-hooks.sh
#
# Safe to re-run. To undo: delete .git/hooks/pre-commit and .git/hooks/pre-push.
set -euo pipefail

MARK="# installed by scripts/install-git-hooks.sh"

cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ "$(git config --get remote.origin.pushurl || true)" == "pushing-is-disabled-on-this-machine" ]]; then
  echo "Pushing is turned off on this machine (scripts/disable-git-push.sh), so there's nothing to check." >&2
  exit 1
fi
if [[ -n "$(git config --get core.hooksPath || true)" ]]; then
  echo "core.hooksPath is set, so git won't run hooks from this clone's .git/hooks. Unset it first." >&2
  exit 1
fi

# The hooks go in this clone's own hooks folder. Others' hooks are left alone.
hooks="$(git rev-parse --git-dir)/hooks"
for hook in pre-commit pre-push; do
  if [[ -e "$hooks/$hook" ]] && ! grep -qF "$MARK" "$hooks/$hook"; then
    echo "$hooks/$hook already exists, so nothing was changed. Move it aside and run this again." >&2
    exit 1
  fi
done

mkdir -p "$hooks"
for hook in pre-commit pre-push; do
  cat >"$hooks/$hook" <<EOF
#!/bin/sh
$MARK
exec bash "\$(git rev-parse --show-toplevel)/scripts/check-secrets.sh" $hook
EOF
  chmod +x "$hooks/$hook"
done
echo "Commits and pushes from this clone are now checked for secrets and personal data."

if [[ ! -f .personal-blocklist ]]; then
  cat >.personal-blocklist <<'EOF'
# Personal details that must never reach GitHub, one per line (not case-sensitive).
# Commits and pushes containing any of them are stopped. This file is git-ignored,
# so it stays on this machine. For example:
#   your Telegram user ID
#   your home address or postcode
#   your Mac's Tailscale or network name
EOF
  echo "Created .personal-blocklist: add personal details to it that should never be pushed."
fi

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "Next, install gitleaks (brew install gitleaks): until then, commits and pushes are stopped." >&2
fi
