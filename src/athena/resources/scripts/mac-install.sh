#!/usr/bin/env bash
# Let the assistant on the Pi use this Mac, over SSH: your Calendar and Reminders through
# iMCP, and your documents, which it indexes so you can search them by meaning. `athena mac
# install` runs it, on the Mac, as yourself, saying what this Mac offers:
#
#   athena mac install --pi-key "ssh-ed25519 AAAA... athena@pi" --calendar
#   athena mac install --pi-key "ssh-ed25519 AAAA... athena@pi" --documents
#
#   --pi-key KEY     the public key that `athena mac connect` printed on the Pi
#   --calendar       Calendar and Reminders, through iMCP
#   --documents      your documents, from folders you choose on Athena's dashboard
#   --server NAME    another server listed in SERVERS below
#   --scope PATH     with --documents, what the Pi may see; repeat for more (default: your
#                    home folder). Never hidden files or folders, nor ~/Library but iCloud Drive.
#   --uninstall      remove everything this script set up
#
# The Pi connects over SSH with its own key, which can only reach these servers: no shell,
# no port forwarding. launchd starts a server for each connection, inside your login
# session, so macOS asks you once for access to your folders and calendars, as it would for
# any app. Safe to re-run: without --calendar or --documents, this Mac offers what it did.
set -euo pipefail

# Where to install the documents server from: the same Athena as the one running this, which
# `athena mac install` says (its git URL and commit, or a folder when developing).
SOURCE="${ATHENA_SOURCE:-}"
APP_DIR="$HOME/Library/Application Support/Athena"
LOG_DIR="$HOME/Library/Logs/Athena"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL=local.athena
KEY='(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/]+={0,3}( [A-Za-z0-9@._-]+)?'

# The servers a Mac can offer, as "name|the command that runs it". To add one, add a line,
# run this script again with --server <name>, and on the Pi add an MCP server with
# command = "ssh" and args = ["athena-mac", "<name>"] (see docs/tools.md).
SERVERS=(
  "documents|\"$APP_DIR/venv/bin/python\" -m athena.mac_documents --scope-file \"$APP_DIR/scope\""
  "apple|/Applications/iMCP.app/Contents/MacOS/imcp-server"
)

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die() {
  printf '\033[1;31m%s\033[0m\n' "$*" >&2
  exit 1
}

pi_key=""
chosen=()
scope=()
uninstall=false
while (($#)); do
  case "$1" in
    --pi-key) pi_key="${2:-}" && shift ;;
    --calendar) chosen+=(apple) ;;
    --documents) chosen+=(documents) ;;
    --server) chosen+=("${2:-}") && shift ;;
    --scope) scope+=("${2:-}") && shift ;;
    --uninstall) uninstall=true ;;
    -h | --help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//' && exit 0 ;;
    *) die "Unknown option: $1 (athena mac install --help)" ;;
  esac
  shift
done

[[ "$(uname)" == Darwin ]] || die "This is for the Mac. On the Pi, use athena mac connect."
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

# Stops and removes a server this script started, by name.
remove_server() {
  launchctl bootout "gui/$uid/$LABEL.$1" 2>/dev/null || true
  rm -f "$AGENTS_DIR/$LABEL.$1.plist" "$APP_DIR/run-$1" "$APP_DIR/$1.sock"
}

if $uninstall; then
  for plist in "$AGENTS_DIR/$LABEL".*.plist; do
    [[ -e "$plist" ]] || continue
    name="${plist##*/"$LABEL".}"
    remove_server "${name%.plist}"
  done
  forget_pi_key
  rm -rf "$APP_DIR"
  echo "Removed: the Pi can no longer reach this Mac's documents, Calendar or Reminders."
  exit 0
fi

if ((${#chosen[@]} == 0)); then
  # Without --calendar or --documents, offer what this Mac did.
  if [[ -s "$APP_DIR/servers" ]]; then
    read -r -a chosen <"$APP_DIR/servers"
  else
    die "Say what this Mac offers: --calendar, --documents, or both."
  fi
fi
((${#scope[@]} == 0)) || [[ " ${chosen[*]} " == *" documents "* ]] || die "--scope is only for --documents."
if [[ -z "$pi_key" ]]; then
  # Without --pi-key, keep the key that's already allowed.
  pi_key="$(grep -sF "$APP_DIR/ssh-command" "$keys" | tail -1 | grep -oE "$KEY\$" || true)"
  [[ -n "$pi_key" ]] || die "The first time, give the Pi's key with --pi-key (athena mac connect on the Pi prints it)."
fi
[[ "$pi_key" =~ ^$KEY$ ]] || die "That doesn't look like a public key. Copy the whole line athena mac connect printed."
[[ "$APP_DIR" != *"'"* ]] || die "Your home folder's path has a ' in it, which this script can't handle."

# The servers this Mac offers, in a stable order, each once.
names=()
known=" "
for server in "${SERVERS[@]}"; do
  name="${server%%|*}"
  known+="$name "
  if [[ " ${chosen[*]} " == *" $name "* ]]; then
    names+=("$name")
  fi
done
for name in "${chosen[@]}"; do
  [[ "$known" == *" $name "* ]] || die "There's no server called '$name' in SERVERS at the top of this script."
done
mkdir -p "$APP_DIR" "$LOG_DIR"
chmod 700 "$APP_DIR"
echo "${names[*]}" >"$APP_DIR/servers"

for plist in "$AGENTS_DIR/$LABEL".*.plist; do
  [[ -e "$plist" ]] || continue
  name="${plist##*/"$LABEL".}"
  name="${name%.plist}"
  [[ " ${names[*]} " == *" $name "* ]] || remove_server "$name"
done

if [[ " ${names[*]} " == *" documents "* ]]; then
  step "Choosing what the Pi may see"
  if ((${#scope[@]})); then
    : >"$APP_DIR/scope.new"
    for folder in "${scope[@]}"; do
      folder="${folder/#\~/$HOME}"
      [[ -d "$folder" ]] || die "Not a folder: $folder"
      (cd "$folder" && pwd) >>"$APP_DIR/scope.new"
    done
    mv "$APP_DIR/scope.new" "$APP_DIR/scope"
  elif [[ ! -s "$APP_DIR/scope" ]]; then
    echo "$HOME" >"$APP_DIR/scope"
  fi
  sed 's/^/  /' "$APP_DIR/scope"
  echo "  (never hidden files or folders, nor ~/Library apart from iCloud Drive)"

  step "Installing the documents server's Python environment"
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  # Its own environment, with a copy of the code rather than a link to it, so nothing else
  # changes what the Pi can do.
  [[ -n "$SOURCE" ]] || die "Run this with athena mac install, which says where to install Athena from."
  [[ -x "$APP_DIR/venv/bin/python" ]] || uv venv --quiet "$APP_DIR/venv"
  uv pip install --quiet --python "$APP_DIR/venv/bin/python" --reinstall-package athena "athena[mac] @ $SOURCE"
else
  rm -f "$APP_DIR/scope"
fi

step "Writing the servers and the SSH command"
for server in "${SERVERS[@]}"; do
  name="${server%%|*}"
  [[ " ${names[*]} " == *" $name "* ]] || continue
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
if [[ " ${names[*]} " == *" apple "* && ! -x /Applications/iMCP.app/Contents/MacOS/imcp-server ]]; then
  ready=false
  warn "iMCP isn't installed, so Calendar and Reminders won't work yet: brew install --cask mattt/tap/iMCP"
fi

printf '\n\033[1;32mDone.\033[0m %s This Mac offers: %s.\n' "$($ready || echo "Fix the warnings above, then:")" "${listed%, }"
if [[ " ${names[*]} " == *" apple "* ]]; then
  cat <<EOF
  - Open iMCP. Turn on Calendar and Reminders only, and allow access when macOS asks. In its
    settings, turn on "Start at login". This Mac needs to stay on and logged in.
EOF
fi
if [[ " ${names[*]} " == *" documents "* ]]; then
  cat <<EOF
  - On Athena's dashboard, open Memory, choose folders and press Index now. macOS asks on this
    Mac's screen for access to them, for "python3": allow it. This Mac only needs to be awake
    and logged in while Athena indexes.
EOF
fi
cat <<EOF
  - On the Pi, check the link with: ssh athena-mac hello (or whichever name you gave this Mac)
    It should say there's no server called 'hello', which means the key works.
EOF
