#!/usr/bin/env bash
# Install pi-assistant's systemd units from deploy/, filling in who runs it and where this repo is:
#
#   pi-assistant.service          Athena itself
#   pi-assistant-stopped.service  tells you in Telegram when Athena has stopped (scripts/tell-stopped.py)
#
# install.sh and update.sh run it. It only writes units that have changed, then has systemd read them
# again. Units changed while Athena runs take effect when it next starts.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_DIR="$(pwd)"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
changed=""
for unit in pi-assistant.service pi-assistant-stopped.service; do
  wanted="$(sed -e "s|@USER@|$USER|g" -e "s|@REPO_DIR@|$REPO_DIR|g" -e "s|@HOME@|$HOME|g" "deploy/$unit")"
  if [[ "$(cat "$UNIT_DIR/$unit" 2>/dev/null || true)" != "$wanted" ]]; then
    printf '%s\n' "$wanted" | sudo tee "$UNIT_DIR/$unit" >/dev/null
    changed="$changed $unit"
  fi
done
if [[ -n "$changed" ]]; then
  sudo systemctl daemon-reload
  echo "Installed$changed"
fi
