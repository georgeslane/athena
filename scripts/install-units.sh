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
# Athena's data folder: data_dir in config.toml, from this folder unless it's a full path. It's the only
# folder Athena may write to, besides uv's and npm's own, so they're made now if they aren't there.
# (It's a top-level key, so before any [section].)
data_dir="$(awk '/^[[:space:]]*\[/ { exit }
  /^[[:space:]]*data_dir[[:space:]]*=/ {
    sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]]*(#.*)?$/, ""); gsub(/^["\047]|["\047]$/, ""); print; exit
  }' config.toml 2>/dev/null || true)"
data_dir="${data_dir:-data}"
case "$data_dir" in "~/"*) data_dir="$HOME/${data_dir#\~/}" ;; esac
[[ "$data_dir" == /* ]] || data_dir="$REPO_DIR/$data_dir"
mkdir -p "$data_dir"
DATA_DIR="$(cd "$data_dir" && pwd -P)"
mkdir -p "$HOME/.cache/uv" "$HOME/.local/share/uv"
changed=""
for unit in pi-assistant.service pi-assistant-stopped.service; do
  wanted="$(sed -e "s|@USER@|$USER|g" -e "s|@REPO_DIR@|$REPO_DIR|g" -e "s|@HOME@|$HOME|g" -e "s|@DATA_DIR@|$DATA_DIR|g" "deploy/$unit")"
  if [[ "$(cat "$UNIT_DIR/$unit" 2>/dev/null || true)" != "$wanted" ]]; then
    printf '%s\n' "$wanted" | sudo tee "$UNIT_DIR/$unit" >/dev/null
    changed="$changed $unit"
  fi
done
if [[ -n "$changed" ]]; then
  sudo systemctl daemon-reload
  echo "Installed$changed"
fi
