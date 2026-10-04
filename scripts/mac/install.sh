#!/usr/bin/env bash
# Let the assistant on the Pi search and read files in folders you choose on this Mac,
# and use your Calendar and Reminders through iMCP. Run it on the Mac, from this repo,
# as yourself:
#
#   bash scripts/mac/install.sh --pi-key "ssh-ed25519 AAAA... athena@pi" --folder ~/Documents
#
#   --pi-key KEY     the public key that scripts/connect-mac.sh printed on the Pi
#   --folder PATH    a folder the assistant may search and read; repeat for more
#   --uninstall      remove everything this script set up
#
# The Pi connects over SSH with its own key, which can only reach these two MCP servers:
# no shell, no port forwarding. launchd starts a server for each connection, inside your
# login session, so macOS asks you once for access to your folders and calendars, as it
# would for any app. Safe to re-run: folders you give replace the old list, and with
# none the list is kept.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$HOME/Library/Application Support/Athena"
LOG_DIR="$HOME/Library/Logs/Athena"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL=local.athena
KEY='(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/]+={0,3}( [A-Za-z0-9@._-]+)?'

# The servers the Pi can reach, as "name|the command that runs it". To add one, add a
# line, run this script again, and on the Pi add an MCP server with command = "ssh" and
# args = ["athena-mac", "<name>"] (see README, "Adding a server").
SERVERS=(
  "files|\"$APP_DIR/venv/bin/python\" -m pi_assistant.mac_files --folders-file \"$APP_DIR/folders\""
  "apple|/Applications/iMCP.app/Contents/MacOS/imcp-server"
)
names=()
for server in "${SERVERS[@]}"; do
  names+=("${server%%|*}")
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die() {
  printf '\033[1;31m%s\033[0m\n' "$*" >&2
  exit 1
}

pi_key=""
folders=()
uninstall=false
while (($#)); do
  case "$1" in
    --pi-key) pi_key="${2:-}" && shift ;;
    --folder) folders+=("${2:-}") && shift ;;
    --uninstall) uninstall=true ;;
    *) die "Unknown option: $1 (see the top of this script)" ;;
  esac
  shift
done

[[ "$(uname)" == Darwin ]] || die "This is for the Mac. On the Pi, use scripts/connect-mac.sh."
uid="$(id -u)"
keys="$HOME/.ssh/authorized_keys"

# Removes the Pi's key, found by the forced command this script gives it.
forget_pi_key() {
  if [[ -f "$keys" ]]; then
    { grep -vF "$APP_DIR/ssh-command" "$keys" || true; } >"$keys.new"
    mv "$keys.new" "$keys"
    chmod 600 "$keys"
  fi
}

if $uninstall; then
  for name in "${names[@]}"; do
    launchctl bootout "gui/$uid/$LABEL.$name" 2>/dev/null || true
    rm -f "$AGENTS_DIR/$LABEL.$name.plist"
  done
  forget_pi_key
  rm -rf "$APP_DIR"
  echo "Removed: the Pi can no longer reach this Mac's files, Calendar or Reminders."
  exit 0
fi

if [[ -z "$pi_key" ]]; then
  # Without --pi-key, keep the key that's already allowed.
  pi_key="$(grep -sF "$APP_DIR/ssh-command" "$keys" | tail -1 | grep -oE "$KEY\$" || true)"
  [[ -n "$pi_key" ]] || die "The first time, give the Pi's key with --pi-key (connect-mac.sh on the Pi prints it)."
fi
[[ "$pi_key" =~ ^$KEY$ ]] || die "That doesn't look like a public key. Copy the whole line connect-mac.sh printed."
[[ "$APP_DIR" != *"'"* ]] || die "Your home folder's path has a ' in it, which this script can't handle."

step "Choosing folders"
mkdir -p "$APP_DIR" "$LOG_DIR"
chmod 700 "$APP_DIR"
if ((${#folders[@]})); then
  : >"$APP_DIR/folders.new"
  for folder in "${folders[@]}"; do
    folder="${folder/#\~/$HOME}"
    [[ -d "$folder" ]] || die "Not a folder: $folder"
    (cd "$folder" && pwd) >>"$APP_DIR/folders.new"
  done
  mv "$APP_DIR/folders.new" "$APP_DIR/folders"
elif [[ ! -s "$APP_DIR/folders" ]]; then
  die "Choose at least one folder with --folder."
fi
sed 's/^/  /' "$APP_DIR/folders"

step "Installing the files server's Python environment"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Its own environment, separate from any you develop in, and a copy of the code rather
# than a link to it, so switching branches here doesn't change what the Pi can do.
UV_PROJECT_ENVIRONMENT="$APP_DIR/venv" uv sync --project "$REPO_DIR" --frozen --no-dev --extra mac --no-editable --quiet

step "Writing the servers and the SSH command"
for server in "${SERVERS[@]}"; do
  name="${server%%|*}"
  [[ "$name" =~ ^[a-z0-9-]+$ ]] || die "Server names can only have lower-case letters, digits and dashes: $name"
  # launchd connects each server's stdin and stdout, and its stderr, to the Pi. So stderr
  # goes to a log first, where it can't garble the conversation.
  cat >"$APP_DIR/run-$name" <<EOF
#!/bin/sh
exec 2>>"$LOG_DIR/$name.log"
exec ${server#*|}
EOF
  chmod 700 "$APP_DIR/run-$name"
done
# sshd runs this, and nothing else, when the Pi connects with its key.
pattern="$(printf '%s | ' "${names[@]}")"
listed="$(printf '%s, ' "${names[@]}")"
cat >"$APP_DIR/ssh-command" <<EOF
#!/bin/sh
case "\$SSH_ORIGINAL_COMMAND" in
  ${pattern% | }) exec nc -U "$APP_DIR/\$SSH_ORIGINAL_COMMAND.sock" ;;
  *)
    echo "athena: there's no server called '\$SSH_ORIGINAL_COMMAND'. The servers are: ${listed%, }." >&2
    exit 1
    ;;
esac
EOF
chmod 700 "$APP_DIR/ssh-command"

step "Starting them with launchd"
xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }
mkdir -p "$AGENTS_DIR"
for name in "${names[@]}"; do
  plist="$AGENTS_DIR/$LABEL.$name.plist"
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL.$name</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$APP_DIR/run-$name")</string>
  </array>
  <key>Sockets</key>
  <dict>
    <key>Listener</key>
    <dict>
      <key>SockPathName</key>
      <string>$(xml "$APP_DIR/$name.sock")</string>
      <key>SockPathMode</key>
      <integer>384</integer>
    </dict>
  </dict>
  <key>inetdCompatibility</key>
  <dict>
    <key>Wait</key>
    <false/>
  </dict>
</dict>
</plist>
EOF
  plutil -lint -s "$plist"
  launchctl bootout "gui/$uid/$LABEL.$name" 2>/dev/null || true
  rm -f "$APP_DIR/$name.sock"
  launchctl bootstrap "gui/$uid" "$plist"
done

step "Letting the Pi's key in, for these servers only"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$keys"
forget_pi_key # one Pi at a time: a new key replaces the old one
# sshd runs the command through your login shell, so the path is quoted: it has a space in it.
echo "restrict,command=\"'$APP_DIR/ssh-command'\" $pi_key" >>"$keys"
chmod 600 "$keys"

ready=true
if ! nc -z -G 2 127.0.0.1 22 >/dev/null 2>&1; then
  ready=false
  warn "Remote Login is off. Turn it on in System Settings > General > Sharing > Remote Login,"
  warn "and under 'Allow access for', choose 'Only these users' and add yourself."
fi
if [[ ! -x /Applications/iMCP.app/Contents/MacOS/imcp-server ]]; then
  ready=false
  warn "iMCP isn't installed, so Calendar and Reminders won't work yet: brew install --cask mattt/tap/iMCP"
fi

cat <<EOF

$(printf '\033[1;32m')Done.$(printf '\033[0m') $($ready || echo "Fix the warnings above, then: ")
  1. Open iMCP. Turn on Calendar and Reminders only, and allow access when macOS asks.
     In its settings, turn on "Start at login".
  2. On the Pi, run:  ssh athena-mac hello
     It should say there's no server called 'hello', which means the key works.
  3. The first time the Pi uses each server, macOS asks on this Mac's screen for access:
     to your folders (for "python3"), and to the local network (for "imcp-server").
     Allow them, over Screen Sharing if this Mac has no screen.

This Mac needs to stay on and logged in for the assistant to reach it.
EOF
