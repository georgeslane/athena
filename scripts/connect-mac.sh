#!/usr/bin/env bash
# Set up the Pi's end of the link to your Mac, for its files, Calendar and Reminders:
# an SSH key that's only for this, and an "athena-mac" entry in ~/.ssh/config. Run it
# on the Pi, from the repo, as your normal user:
#
#   bash scripts/connect-mac.sh <the Mac's address> <your user name on the Mac>
#   bash scripts/connect-mac.sh my-mac.tail1234.ts.net georges
#
# Then run the command it prints on the Mac (scripts/mac/install.sh). Safe to re-run.
set -euo pipefail

host="${1:-}"
user="${2:-}"
if [[ ! "$host" =~ ^[A-Za-z0-9._:-]+$ || ! "$user" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Usage: bash scripts/connect-mac.sh <the Mac's address> <your user name on the Mac>" >&2
  exit 2
fi

key="$HOME/.ssh/athena_mac"
config="$HOME/.ssh/config"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if [[ ! -f "$key" ]]; then
  # No passphrase, because the assistant uses it unattended. On the Mac it can only
  # reach the assistant's two servers (see scripts/mac/install.sh).
  ssh-keygen -q -t ed25519 -N "" -C "athena@$(hostname)" -f "$key"
fi

touch "$config"
chmod 600 "$config"
if grep -qx 'Host athena-mac' "$config"; then
  echo "$config already has an athena-mac entry, so it was left alone."
else
  cat >>"$config" <<EOF

# The assistant's link to your Mac (scripts/connect-mac.sh)
Host athena-mac
  HostName $host
  User $user
  IdentityFile ~/.ssh/athena_mac
  IdentitiesOnly yes
  BatchMode yes
  StrictHostKeyChecking accept-new
  ConnectTimeout 10
  ServerAliveInterval 30
  ServerAliveCountMax 3
EOF
fi

cat <<EOF
Now, on the Mac, from the repo, choosing the folders the assistant may read:

  bash scripts/mac/install.sh --pi-key "$(cat "$key.pub")" --folder ~/Documents

Then back here, check the link: ssh athena-mac hello
It should say there's no server called 'hello'.
EOF
