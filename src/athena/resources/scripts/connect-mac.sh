#!/usr/bin/env bash
# Set up the Pi's end of the link to one of your Macs: an SSH key that's only for this (one
# for all your Macs), and an entry in ~/.ssh/config that names the Mac. `athena mac connect`
# runs it, on the Pi, as your normal user:
#
#   athena mac connect <the Mac's address> <your user name on the Mac> [name]
#   athena mac connect my-mac.tail1234.ts.net georges             # athena-mac
#   athena mac connect my-laptop.tail1234.ts.net georges laptop   # athena-laptop
#
# Athena reaches Calendar and Reminders on "athena-mac" and your documents on "athena-laptop",
# unless you change that in config.toml. Then run the command it prints on the Mac
# (athena mac install), which installs the same version of Athena there: $ATHENA_SOURCE, which
# `athena mac connect` sets. Safe to re-run.
set -euo pipefail

host="${1:-}"
user="${2:-}"
name="${3:-mac}"
if [[ ! "$host" =~ ^[A-Za-z0-9._:-]+$ || ! "$user" =~ ^[A-Za-z0-9._-]+$ || ! "$name" =~ ^[a-z0-9-]+$ || $# -gt 3 ]]; then
  echo "Usage: athena mac connect <the Mac's address> <your user name on the Mac> [name, such as laptop]" >&2
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

# The assistant's link to one of your Macs (athena mac connect)
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
Now, on that Mac, install Athena's servers, saying what it offers: --calendar for Calendar
and Reminders, --documents for your documents, or both. With uv on the Mac:

  uvx --from "${ATHENA_SOURCE:-git+https://github.com/georgeslane/athena}" athena mac install --pi-key "$(cat "$key.pub")" --documents

Then back here, check the link: ssh $alias hello
It should say there's no server called 'hello'.
EOF
