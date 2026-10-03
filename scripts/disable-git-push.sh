#!/usr/bin/env bash
# Stop this clone from pushing anywhere. install.sh runs it on the Pi, so nothing
# that lives there (.env, config.toml, the memory database) can be pushed to
# GitHub, even by mistake. `git pull` still works for updates.
#
#   bash scripts/disable-git-push.sh
#
# To undo (e.g. to develop on this machine): git config --unset-all remote.origin.pushurl
# and delete .git/hooks/pre-push.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not a git clone; nothing to disable."
  exit 0
fi

# `git push` to origin goes nowhere...
if git remote get-url origin >/dev/null 2>&1; then
  git config --replace-all remote.origin.pushurl "pushing-is-disabled-on-this-machine"
fi

# ...and pushes to any other remote or URL are refused by a hook. It goes in this
# clone's own hooks folder, never a shared core.hooksPath.
hook="$(git rev-parse --git-dir)/hooks/pre-push"
mkdir -p "$(dirname "$hook")"
cat >"$hook" <<'EOF'
#!/bin/sh
echo "Pushing from this machine is disabled (see scripts/disable-git-push.sh)." >&2
exit 1
EOF
chmod +x "$hook"
echo "git push is disabled for this clone."
