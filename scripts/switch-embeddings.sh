#!/usr/bin/env bash
# Switch Athena's embeddings model. It downloads the model, tests it against the one you use
# now and shows what would change. If you agree, it re-embeds every memory with it. Athena is
# stopped while that runs and started again afterwards, whatever happens.
#
#   bash scripts/switch-embeddings.sh embeddinggemma-2:740m-bf16
set -euo pipefail

main() {
  local model="${1:-}"
  if [ -z "$model" ] || [ $# -gt 1 ]; then
    echo "Usage: bash scripts/switch-embeddings.sh MODEL   (for example embeddinggemma-2:740m-bf16)" >&2
    exit 2
  fi
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  export PATH="$HOME/.local/bin:$PATH"
  PULL_LOG="$(mktemp)"
  trap cleanup EXIT

  if ! pull "$model"; then
    if ! grep -qi "newer version" "$PULL_LOG"; then
      echo "Couldn't download $model, so nothing was changed." >&2
      exit 1
    fi
    read -r -p "$model needs a newer Ollama. Update Ollama now? [y/N] " answer || answer=""
    case "$answer" in
      y | Y | yes | Yes) ;;
      *) echo "Nothing was changed." >&2; exit 1 ;;
    esac
    curl -fsSL https://ollama.com/install.sh | sh
    for _ in $(seq 1 30); do
      curl -sf http://127.0.0.1:11434/api/version >/dev/null && break
      sleep 1
    done
    pull "$model" || { echo "Couldn't download $model, so nothing was changed." >&2; exit 1; }
  fi

  sudo systemctl stop pi-assistant
  STOPPED=1
  if uv run pi-assistant embeddings use "$model"; then
    # Ollama keeps models loaded (OLLAMA_KEEP_ALIVE=-1): restarting it lets go of the old one.
    sudo systemctl restart ollama
  else
    exit 1
  fi
}

PULL_LOG=""
STOPPED=""

pull() {
  ollama pull "$1" 2>&1 | tee "$PULL_LOG"
}

cleanup() {
  rm -f "$PULL_LOG"
  if [ -n "$STOPPED" ]; then
    sudo systemctl start pi-assistant && echo "Started Athena again."
  fi
}

main "$@"
