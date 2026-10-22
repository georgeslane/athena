#!/usr/bin/env bash
# Switch Athena's embeddings model. It tests the model against the one you use now and shows
# what would change. If you agree, it re-embeds every memory with it. Athena is stopped while
# that runs and started again afterwards, whatever happens.
#
#   athena embeddings switch embeddinggemma-2-8bit             # from wherever embeddings come from now
#   athena embeddings switch embeddinggemma-2-bf16 --on-mac    # moving them to oMLX on the Mac
#
# $ATHENA is the athena command, which `athena embeddings switch` sets.
#
# With Ollama on the Pi, the model is downloaded first. A server elsewhere, such as oMLX on the
# Mac, has its own models: download it there.
set -euo pipefail

main() {
  local model="" on_mac="" download=""
  for arg in "$@"; do
    case "$arg" in
      --on-mac) on_mac=1 ;;
      -*) usage ;;
      *) if [ -z "$model" ]; then model="$arg"; else usage; fi ;;
    esac
  done
  [ -n "$model" ] || usage
  export PATH="$HOME/.local/bin:$PATH"
  ATHENA="${ATHENA:-athena}"
  PULL_LOG="$(mktemp)"
  trap cleanup EXIT

  if [ -z "$on_mac" ] && [ "$("$ATHENA" embeddings where)" = ollama ]; then
    download=1
  fi
  if [ -n "$download" ] && ! pull "$model"; then
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

  sudo systemctl stop athena
  STOPPED=1
  if ! "$ATHENA" embeddings use "$model" ${on_mac:+--on-mac}; then
    exit 1
  fi
  if [ -n "$download" ]; then
    # Ollama keeps models loaded (OLLAMA_KEEP_ALIVE=-1): restarting it lets go of the old one.
    sudo systemctl restart ollama
  fi
}

usage() {
  echo "Usage: athena embeddings switch MODEL [--on-mac]   (for example embeddinggemma-2-bf16 --on-mac)" >&2
  exit 2
}

PULL_LOG=""
STOPPED=""

pull() {
  ollama pull "$1" 2>&1 | tee "$PULL_LOG"
}

cleanup() {
  rm -f "$PULL_LOG"
  if [ -n "$STOPPED" ]; then
    sudo systemctl start athena && echo "Started Athena again."
  fi
}

main "$@"
