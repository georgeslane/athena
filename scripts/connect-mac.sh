#!/usr/bin/env bash
# Set up the Pi's end of the link to one of your Macs: an SSH key that's only for this (one
# for all your Macs), and an entry in ~/.ssh/config that names the Mac. Run it on the Pi,
# from the repo, as your normal user:
#
#   bash scripts/connect-mac.sh <the Mac's address> <your user name on the Mac> [name]
#   bash scripts/connect-mac.sh my-mac.tail1234.ts.net georges             # athena-mac
#   bash scripts/connect-mac.sh my-laptop.tail1234.ts.net georges laptop   # athena-laptop
#
# Athena reaches Calendar and Reminders on "athena-mac" and your documents on "athena-laptop",
# unless you change that in config.toml. Then run the command it prints on the Mac
# (scripts/mac/install.sh). Safe to re-run.
set -euo pipefail

host="${1:-}"
user="${2:-}"
name="${3:-mac}"
if [[ ! "$host" =~ ^[A-Za-z0-9._:-]+$ || ! "$user" =~ ^[A-Za-z0-9._-]+$ || ! "$name" =~ ^[a-z0-9-]+$ || $# -gt 3 ]]; then
  echo "Usage: bash scripts/connect-mac.sh <the Mac's address> <your user name on the Mac> [name, such as laptop]" >&2
  exit 2
fi
alias="athena-$name"

key="$HOME/.ssh/athena_mac"
config="$HOME/.ssh/config"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if [[ ! -f "$key" ]]; then
  # No passphrase, because the assistant uses it unattended. On each Mac it can only
  # reach the assistant's servers there (see scripts/mac/install.sh).
  ssh-keygen -q -t ed25519 -N "" -C "athena@$(hostname)" -f "$key"
fi

touch "$config"
chmod 600 "$config"
if grep -qx "Host $alias" "$config"; then
  echo "$config already has an $alias entry, so it was left alone."
else
  cat >>"$config" <<EOF

# The assistant's link to one of your Macs (scripts/connect-mac.sh)
Host $alias
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
Now, on that Mac, from the repo, saying what it offers: --calendar for Calendar and
Reminders, --documents for your documents, or both:

  bash scripts/mac/install.sh --pi-key "$(cat "$key.pub")" --documents

Then back here, check the link: ssh $alias hello
It should say there's no server called 'hello'.
EOF
