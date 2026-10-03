#!/usr/bin/env bash
# Update pi-assistant: pull the latest code, update its dependencies and restart it.
# Run it from the repo, as your normal user, over SSH:
#
#   bash scripts/update.sh
set -euo pipefail

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  export PATH="$HOME/.local/bin:$PATH"
  git pull --ff-only
  services=(pi-assistant)
  if systemctl cat pi-assistant-display.service >/dev/null 2>&1; then
    # Keep the status board's packages: a plain `uv sync` removes them.
    uv sync --no-dev --extra display
    services+=(pi-assistant-display)
  else
    uv sync --no-dev
  fi
  sudo systemctl restart "${services[@]}"
  echo "Updated and restarted: ${services[*]}"
}

# Everything runs inside main(), which bash reads in full first, so `git pull` can
# safely change this file while it runs.
main "$@"
exit
