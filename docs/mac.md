# Your Mac

Athena can use your Calendar and Reminders, and your documents, on your Macs. It reaches them over SSH, with a key of its own that each Mac limits to Athena's servers there: no shell, no port forwarding, nothing else. On each Mac, launchd starts a server for each connection inside your login session, so macOS asks for access to your calendars or folders as it would for any app. Each Mac offers what you choose:

- **Calendar and Reminders**, through [iMCP](https://github.com/mattt/iMCP), on a Mac that's always on, such as the one running oMLX. The Pi calls it `athena-mac`.
- **Your documents**, on the Mac they're on, such as your laptop. Athena reads them only when you index them. The Pi calls it `athena-laptop`. See [Documents](memory.md#documents).

One Mac can offer both.

## Set it up

1. **On the Pi**, make the key (just once) and an SSH entry for each Mac, using its name on your tailnet and your user name on it. Name the one with your documents `laptop`:

   ```bash
   athena mac connect my-mac.tail1234.ts.net georges            # athena-mac
   athena mac connect my-laptop.tail1234.ts.net georges laptop  # athena-laptop
   ```

   Each prints the command for step 3, with the Pi's public key in it.

2. **On each Mac**, turn on Remote Login: System Settings > General > Sharing > Remote Login, allowing only your user. Install [uv](https://docs.astral.sh/uv/) if it isn't there: `curl -LsSf https://astral.sh/uv/install.sh | sh`. For Calendar, also install iMCP: `brew install --cask mattt/tap/iMCP`.

3. **On each Mac**, run the command step 1 printed, saying what that Mac offers, `--calendar`, `--documents` or both. It installs the same version of Athena as the Pi's:

   ```bash
   uvx --from "git+https://github.com/georgeslane/athena@<the Pi's version>" athena mac install \
     --pi-key "ssh-ed25519 AAAA... athena@pi" --calendar
   ```

4. **For Calendar**, open iMCP. Turn on Calendar and Reminders only, allow access when macOS asks, and turn on "Start at login". That Mac needs to stay on and logged in.

5. **On the Pi**, check each link: `ssh athena-mac hello` should say there's no server called 'hello', which means the key works. Then switch on **Calendar and Reminders** on the dashboard's Tools tab, choose folders under **Memory**, and run `athena doctor`.

6. **The first time** the Pi uses each server, macOS asks on that Mac for access: to the local network for "imcp-server", and to your folders for "python3". Allow them, over Screen Sharing if the Mac has no screen.

After updating Athena on the Pi, run `athena mac connect` there again, and the command it prints on each Mac: without `--calendar` or `--documents`, it keeps what that Mac offers. With `--uninstall`, it removes everything, including the Pi's key.

## What the Pi can see

This is decided on the Mac, not the Pi:

- By default, only what's in your home folder. To narrow it, add `--scope ~/Documents` to `--documents`; repeat it for more folders.
- Never hidden files or folders, such as `.ssh`, nor anything in `~/Library` apart from iCloud Drive.
- Folders inside apps and code (`.app` bundles, `node_modules`) are skipped.
- A file that's only in iCloud, not downloaded, is skipped rather than downloaded.
- It can't change, move or delete anything.

**A laptop with Remote Login on** can be reached on any network it joins, and macOS lets you sign in over SSH with your password as well as a key. To allow keys only, for every SSH login, run this on it:

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo tee /etc/ssh/sshd_config.d/050-keys-only.conf
```

## Adding a server on the Mac

A server that needs your Mac's apps or files runs on the Mac. Add a line to `SERVERS` at the top of `src/athena/resources/scripts/mac-install.sh`, then run `athena mac install --server <its name>` on the Mac from a checkout with that change. On the Pi, add a server with `command = "ssh"` and `args = ["athena-mac", "<its name>"]`.
