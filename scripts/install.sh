#!/usr/bin/env bash
# Set up pi-assistant on a Raspberry Pi running 64-bit Raspberry Pi OS (or any
# Debian-based Linux). Run it from the repo, as your normal user, over SSH:
#
#   bash scripts/install.sh              # the assistant
#   bash scripts/install.sh --display    # plus the status board on a Pimoroni Display HAT Mini
#
# Safe to re-run: it skips what's already installed and never overwrites your
# config.toml or .env.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EMBED_MODEL="${EMBED_MODEL:-embeddinggemma}"
SERVICE=/etc/systemd/system/pi-assistant.service
DISPLAY_SERVICE=/etc/systemd/system/pi-assistant-display.service

WITH_DISPLAY=false
for arg in "$@"; do
  case "$arg" in
    --display) WITH_DISPLAY=true ;;
    *)
      echo "Unknown option: $arg (the only option is --display)" >&2
      exit 2
      ;;
  esac
done
# Once the status board is set up, re-runs keep it.
if systemctl cat pi-assistant-display.service >/dev/null 2>&1; then
  WITH_DISPLAY=true
fi

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not root (it uses sudo where needed)." >&2
  exit 1
fi
if [[ "$(uname -m)" != "aarch64" ]]; then
  warn "Expected a 64-bit OS (aarch64), found $(uname -m). Continuing anyway."
fi

step "Installing system packages"
packages=(git curl ca-certificates sqlite3)
if $WITH_DISPLAY; then
  packages+=(gcc libc6-dev) # the display's SPI driver (spidev) is built from source
fi
sudo apt-get update -qq
sudo apt-get install -y -qq "${packages[@]}" >/dev/null

step "Installing uv (Python package manager)"
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

step "Installing Ollama (serves the embeddings model locally)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
# Keep the embeddings model loaded and only listen on localhost.
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=127.0.0.1:11434"\nEnvironment="OLLAMA_KEEP_ALIVE=-1"\n' |
  sudo tee /etc/systemd/system/ollama.service.d/pi-assistant.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ollama >/dev/null 2>&1 || true
sudo systemctl restart ollama
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:11434/api/version >/dev/null && break
  sleep 1
done
ollama pull "$EMBED_MODEL"

step "Installing Python dependencies"
cd "$REPO_DIR"
if $WITH_DISPLAY; then
  uv sync --no-dev --extra display
else
  uv sync --no-dev
fi

step "Pre-fetching the example MCP servers"
uvx mcp-server-time --help >/dev/null 2>&1 || warn "couldn't pre-fetch mcp-server-time"
uvx mcp-server-fetch --help >/dev/null 2>&1 || warn "couldn't pre-fetch mcp-server-fetch"

step "Disabling git push (secrets and memories live in this folder)"
bash scripts/disable-git-push.sh

step "Creating config files"
mkdir -p data
if [[ ! -f config.toml ]]; then
  cp config.example.toml config.toml
  echo "Created config.toml"
else
  echo "config.toml already exists; left it alone"
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env"
fi
chmod 600 .env

step "Installing the systemd service"
sed -e "s|@USER@|$USER|g" -e "s|@REPO_DIR@|$REPO_DIR|g" -e "s|@HOME@|$HOME|g" \
  deploy/pi-assistant.service | sudo tee "$SERVICE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable pi-assistant >/dev/null 2>&1

if $WITH_DISPLAY; then
  step "Setting up the status board (Display HAT Mini)"
  if command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_spi 0 # turns SPI on straight away, no reboot needed
  else
    warn "raspi-config not found: turn on SPI yourself (dtparam=spi=on)"
  fi
  for group in spi gpio; do
    if getent group "$group" >/dev/null; then
      sudo usermod -aG "$group" "$USER"
    fi
  done
  sed -e "s|@USER@|$USER|g" -e "s|@REPO_DIR@|$REPO_DIR|g" \
    deploy/pi-assistant-display.service | sudo tee "$DISPLAY_SERVICE" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable pi-assistant-display >/dev/null 2>&1
  sudo systemctl restart pi-assistant-display
  echo "The status board is running. It shows \"offline\" until the assistant starts."
fi

cat <<EOF

$(printf '\033[1;32m')Done.$(printf '\033[0m') Next steps (all in $REPO_DIR):

  1. Secrets:   nano .env           (TELEGRAM_BOT_TOKEN, LLM_API_KEY)
  2. Settings:  nano config.toml    (llm.base_url, llm.model, agent.user_name, timezone)
  3. Check:     uv run pi-assistant doctor
  4. Try it:    uv run pi-assistant chat
  5. Start:     sudo systemctl start pi-assistant
     Logs:      journalctl -u pi-assistant -f

Then message your bot on Telegram. It replies with your user ID: put that in
telegram.allowed_user_ids in config.toml and run: sudo systemctl restart pi-assistant
EOF
