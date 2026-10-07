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
  uv sync --no-dev
  if ! dpkg -s bubblewrap >/dev/null 2>&1; then
    # Local MCP servers run in a bubblewrap sandbox, and won't start without it.
    sudo apt-get install -y -qq bubblewrap >/dev/null
  fi
  sudo systemctl restart pi-assistant
  echo "Updated and restarted pi-assistant."
  if systemctl cat pi-assistant-display.service >/dev/null 2>&1; then
    # The status board that used to be part of pi-assistant. It's now pi-display-microservice.
    sudo systemctl disable --now pi-assistant-display >/dev/null 2>&1 || true
    sudo rm -f /etc/systemd/system/pi-assistant-display.service
    sudo systemctl daemon-reload
    echo "Stopped the old status board: it's now its own project, pi-display-microservice."
    echo "To keep using the screen, set that up (see README, \"Status board\")."
  fi
}

# Everything runs inside main(), which bash reads in full first, so `git pull` can
# safely change this file while it runs.
main "$@"
exit
