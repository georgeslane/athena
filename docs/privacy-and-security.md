# Privacy and security

## Privacy

Before a tool runs, Athena looks through everything it would send for:

- **your private words:** ones you list on the dashboard's Memory tab, such as your full name, address, family's names and employer, and your name as `[agent] user_name` has it
- **what identifies anyone:** email addresses (but not example ones, like `@example.com`), phone numbers, postcodes, card and bank account numbers, National Insurance numbers, street addresses and precise coordinates
- **Athena's secrets:** the values in `.env`, and anything shaped like an API key or token

What it finds decides what happens:

- **A secret** stops any tool, even one that doesn't ask first.
- **Personal details** stop the tools in `[privacy] block`: by default, those that send to Claude's projects. The model is told what it found, and to take it out and try again.
- **Any other tool that asks first** starts its approval message with what it found, so you see it before you allow it.

**Try it** on the Memory tab shows what would be found. Add tools, or patterns like `post_*`, to `block` in `config.toml` to stop them too. The check can't tell your details from someone else's, so a task that happens to contain a postcode is stopped too, and the model rewrites it.

## Security

**Your data stays with you.**
- The model runs on your own server. Telegram bot chats aren't end-to-end encrypted, though, so messages and photos pass through Telegram's servers.
- Only allowlisted Telegram users can use the bot. Messages from anyone else are ignored and logged.
- Secrets are in `~/.athena/.env`, readable only by you. The dashboard can't change Athena's own secrets (the Telegram token, the model's key, the Siri token and its own password), or pass them to a server, and keys you enter are never sent back to the browser.

**Tools ask before they act.**
- Tools that could change something or send your data off your network ask first, showing their full arguments. Only set `confirm = []` on servers that can't send data off your network.
- Text from tools, such as web pages, is untrusted: it can tell the model to send your data somewhere. The approval is what stops that.
- A server's tools that are new or have changed since you approved them are held back until you approve them again.
- What Athena remembers, and skills it writes or changes, while working by itself are suggestions until you approve them, so a web page can't plant a memory or rewrite a skill unseen.
- Tasks can't do more than you could approve: one Athena suggests needs your OK, any tool that asks first still asks unless you've allowed that task to use it, and a task can't schedule others without asking.

**Everything runs in a box.**
- systemd runs Athena hardened. It can write only to `~/.athena` and uv's and npm's caches. It can't change its own code, your home folder or `~/.ssh`, and can't use `sudo` or gain privileges.
- Local MCP servers run in a sandbox without your home folder, so they can't read `.env`, your memories or your SSH keys, and without the network if they don't need it. A server with network access can reach anything the Pi can, so the approval is still what stops your data being sent somewhere.
- Code Athena writes runs in a stricter sandbox, with no network, almost no environment, and only its own folder to write to.
- Servers started with `uvx` run from locked environments: every package's version and hash is fixed, nothing is built from source, new installs are checked for known malware, and packages are checked for known vulnerabilities every day.

**Nothing can be pushed from the Pi but what you allow.**
- There's no git checkout on the Pi: Athena's installed by uv, and its settings and data are in `~/.athena`, outside any repository.
- The Pi can push to one GitHub repository only, for [Projects for Claude](claude-projects.md), if you set it up. Its token reaches that repo alone, Athena refuses to push if it's public, and each push asks you first in Telegram, showing everything it contains. A task can't be allowed to skip that.

**Reaching Athena:**
- The dashboard, the Siri endpoint and the status API listen only on the Pi itself. Tailscale Serve passes on requests from your tailnet, and from nowhere else.
- Everything on the dashboard but the sign-in page needs its password. Changes are only accepted from its own page, so another website can't make them with your cookie. Anyone with the password can add an MCP server, which runs a command on the Pi, so keep it as private as an SSH password.
- The Siri endpoint is off unless you turn it on, and each request needs its token.
- The status API includes the start of your latest message, for the status board. If other people can see the screen, set `show_task = false` under `[display]`.

**Specific tools:**
- The Pi reaches your Macs over SSH with a key of its own, which each Mac restricts to starting Athena's servers: no shell, no port forwarding. The documents server is read-only, and never sees hidden files or `~/Library` apart from iCloud Drive.
- The email server only sends to addresses you allow, and can't delete or move mail, even if you approve. The GitHub token can only read public repositories.
- Placing or cancelling a Trading 212 order always asks, and that can't be switched off. The key's own permissions have the last word.
- SQLite databases are opened read-only, and SQLite itself refuses anything but reading.
