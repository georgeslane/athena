"""`athena setup`: setting Athena up in the terminal, and changing its main settings later.

The first time, it makes Athena's home folder (~/.athena), with config.toml and .env from resources/, offers to
move in an Athena that ran from a git checkout, and goes through each part in turn: you, the model, memory,
Telegram and the dashboard, then installs the service. After that it shows each part with how it's set, to
change any of them. Everything else is in config.toml, or on the dashboard's Tools tab.

It writes config.toml and .env as the dashboard does (config_files.py): only what changes, keeping comments.
Secrets go in .env, and are never shown: only whether they're set.
"""

from __future__ import annotations

import getpass
import os
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv

from athena import service
from athena.config import athena_home, find_config, unsendable
from athena.config_files import EnvFile, write_private
from athena.resources import CONFIG_EXAMPLE, ENV_EXAMPLE
from athena.settings import Settings, SettingsError

TELEGRAM = os.environ.get("TELEGRAM_API", "https://api.telegram.org")  # another address, for the tests
WAIT_FOR_MESSAGE = 120  # seconds: how long it waits for your message to the bot, to learn your Telegram ID


@dataclass
class Part:
    key: str
    title: str
    show: Callable[[Wizard], str]  # how it's set now, in a few words
    change: Callable[[Wizard], None]


class Wizard:
    def __init__(
        self,
        ask: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass,
        say: Callable[[str], None] = print,
        http: httpx.Client | None = None,
        run: Callable[..., Any] | None = None,
        config: str | Path | None = None,
    ):
        self.ask_raw, self.secret_raw, self.say = ask, secret, say
        self.http = http or httpx.Client(timeout=15)
        self.run = run or subprocess.run  # for the service's commands
        self.path = find_config(config)
        self.settings = Settings(self.path, self.path.parent / ".env")
        self.changed = False  # whether a setting or secret has been saved
        load_dotenv(self.path.parent / ".env", override=False, interpolate=False)  # which secrets are set

    # -- asking --

    def ask(self, question: str, now: str = "") -> str:
        """An answer, or what it is now if you just press Enter."""
        answer = self.ask_raw(f"{question}{f' [{now}]' if now else ''}: ").strip()
        return answer or now

    def yes(self, question: str, default: bool = True) -> bool:
        answer = self.ask_raw(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer.startswith("y")

    def ask_secret(self, question: str, name: str) -> str | None:
        """A secret for .env, typed without showing it. None to keep what's there."""
        is_set = bool(os.environ.get(name))
        answer = self.typed_secret(f"{question}{' [set: Enter keeps it]' if is_set else ''}: ")
        return answer or (None if is_set else "")

    def typed_secret(self, prompt: str) -> str:
        """A secret, asked for again while it has a character that couldn't be sent."""
        while True:
            answer = self.secret_raw(prompt).strip()
            bad = unsendable(answer)
            if bad is None:
                return answer
            self.say(f"  That has a {bad!r} in it, which can't be: perhaps it was copied with it. Try again.")

    # -- what's in the files --

    def raw(self) -> dict[str, Any]:
        return tomllib.loads(self.path.read_text())

    def get(self, dotted: str, default: Any = "") -> Any:
        node: Any = self.raw()
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, values: dict[str, Any]) -> bool:
        """Save settings in config.toml. False, saying why, if they'd make it invalid."""
        try:
            self.settings.set_values(values)
        except SettingsError as exc:
            self.say(f"  Not saved: {exc}")
            return False
        self.changed = True
        return True

    def set_secrets(self, values: dict[str, str | None]) -> None:
        changes = {name: value for name, value in values.items() if value is not None}
        if changes:
            EnvFile(self.path.parent / ".env").update(changes)
            self.changed = True

    # -- the whole thing --

    def start(self) -> None:
        home = self.path.parent
        self.say(f"Athena setup. Its settings, secrets and data are in {home}.\n")
        if sys.platform != "linux":
            self.say("Athena runs on Linux, such as on a Raspberry Pi. Here you can only set up its files.\n")
        first = not self.path.exists()
        if first:
            self.make_home()
        else:
            for change in self.settings.upgrade():  # from an older Athena: it wouldn't save otherwise
                self.say(f"config.toml: {change}")
        parts = PARTS
        if first:
            for part in parts:
                self.say(f"\n== {part.title}")
                part.change(self)
            self.say("\nAll set. Run `athena setup` again to change any of this, and `athena doctor` to check it.")
            return
        while True:
            self.say("")
            for i, part in enumerate(parts, 1):
                self.say(f"  {i}  {part.title:<22} {part.show(self)}")
            choice = self.ask_raw("\nChange which? (number, or Enter when you're done): ").strip()
            if not choice:
                if self.changed:
                    self.offer_restart()
                return
            if not choice.isdigit() or not 1 <= int(choice) <= len(parts):
                self.say("  Choose one of the numbers.")
                continue
            part = parts[int(choice) - 1]
            self.say(f"\n== {part.title}")
            part.change(self)

    def offer_restart(self) -> None:
        """Athena reads its settings and secrets when it starts, so restart it, if it's running, to use new ones."""
        if sys.platform != "linux" or not shutil.which("systemctl"):
            return
        if self.run(["systemctl", "is-active", "--quiet", "athena"]).returncode != 0:
            return
        if self.yes("Athena's running with what it had before. Restart it to use the changes?"):
            self.run(["sudo", "systemctl", "restart", "athena"], check=True)
            self.say("  Restarted.")
        else:
            self.say("  It'll use them when it next starts: `sudo systemctl restart athena`.")

    def make_home(self) -> None:
        """Athena's home folder, with config.toml and .env to start from, or moved from an old checkout."""
        home = self.path.parent
        checkout = service.old_checkout() if home == athena_home() else None
        if checkout and self.yes(f"Athena was set up in {checkout}. Move its settings, secrets and data here?"):
            for done in service.move_from(checkout, self.run):
                self.say(f"  {done}")
            changed = self.settings.upgrade()
            for change in changed:
                self.say(f"  config.toml: {change}")
            self.say(f"  When Athena's running from here, you can delete {checkout}.")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            write_private(self.path, CONFIG_EXAMPLE.read_text(), 0o600)
        env = home / ".env"
        if not env.exists():
            write_private(env, ENV_EXAMPLE.read_text(), 0o600)


# -- the parts -----------------------------------------------------------------------------------------


def _show_you(w: Wizard) -> str:
    return f"{w.get('agent.user_name')} · {w.get('agent.assistant_name')} · {w.get('agent.timezone')}"


def _change_you(w: Wizard) -> None:
    user = w.ask("Your first name", _unless(w.get("agent.user_name"), "the user"))
    name = w.ask("What to call the assistant", w.get("agent.assistant_name", "Athena"))
    while True:
        zone = w.ask("Your time zone, like Europe/London", w.get("agent.timezone") or _system_zone())
        try:
            ZoneInfo(zone)
            break
        except (ZoneInfoNotFoundError, ValueError):
            w.say("  That isn't a time zone: use one like Europe/London or America/New_York.")
    w.set({"agent.user_name": user, "agent.assistant_name": name, "agent.timezone": zone})


def _show_model(w: Wizard) -> str:
    return f"{w.get('llm.model')} at {w.get('llm.base_url')}"


def _change_model(w: Wizard) -> None:
    w.say("The model runs on an OpenAI-compatible server, such as oMLX on a Mac.")
    url = w.ask("Its address", w.get("llm.base_url")).rstrip("/")
    key = w.ask_secret("Its API key (Enter if it has none)", "LLM_API_KEY")
    w.set_secrets({"LLM_API_KEY": key})
    models = _models(w, url, key if key is not None else os.environ.get("LLM_API_KEY", ""))
    model = _choose(w, "Which model", models, w.get("llm.model"))
    w.set({"llm.base_url": url, "llm.model": model})


def _show_memory(w: Wizard) -> str:
    where = w.get("embeddings.base_url") or "the model's server"
    return f"{w.get('embeddings.model')} on {where}"


def _change_memory(w: Wizard) -> None:
    w.say("Memories are searched by meaning with an embeddings model, usually on the same server as the model.")
    url = w.ask("Its server's address (Enter for the model's)", w.get("embeddings.base_url")).rstrip("/")
    if url == w.get("llm.base_url").rstrip("/"):
        url = ""  # the model's, with its key, and still the model's if that moves
    base = url or w.get("llm.base_url")
    models = [m for m in _models(w, base, os.environ.get("LLM_API_KEY", ""), quiet=True) if "embed" in m.lower()]
    model = _choose(w, "Which embeddings model", models, w.get("embeddings.model"))
    w.set({"embeddings.base_url": url, "embeddings.model": model})


def _show_telegram(w: Wizard) -> str:
    users = w.get("telegram.allowed_user_ids", [])
    token = "bot token set" if os.environ.get("TELEGRAM_BOT_TOKEN") else "no bot token"
    return f"{token}, {len(users)} user{'' if len(users) == 1 else 's'} allowed"


def _change_telegram(w: Wizard) -> None:
    w.say("You talk to Athena through a Telegram bot of your own: make one with @BotFather (/newbot).")
    token = w.ask_secret("The bot's token", "TELEGRAM_BOT_TOKEN")
    w.set_secrets({"TELEGRAM_BOT_TOKEN": token})
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    bot = _telegram(w, token, "getMe")
    if bot is None:
        w.say("  Telegram didn't accept that token: check it with @BotFather.")
        return
    w.say(f"  That's @{bot.get('username')}.")
    users = list(w.get("telegram.allowed_user_ids", []))
    if users and not w.yes(f"Only these Telegram users may talk to it: {users}. Change that?", default=False):
        return
    you = _your_id(w, token, bot.get("username", "your bot"))
    if you is None:
        typed = w.ask("Your Telegram user ID (a number: @userinfobot tells you)")
        you = int(typed) if typed.isdigit() else None
    if you is not None:
        w.set({"telegram.allowed_user_ids": [you]})
        w.say(f"  Only you ({you}) may talk to it.")


def _show_dashboard(w: Wizard) -> str:
    return "password set" if os.environ.get("DASHBOARD_TOKEN") else "no password yet"


def _change_dashboard(w: Wizard) -> None:
    w.say("The dashboard is a web page for Athena's status, tasks, memory and tools, behind a password.")
    if os.environ.get("DASHBOARD_TOKEN") and not w.yes("It has a password. Choose a new one?", default=False):
        return
    typed = w.typed_secret("A password of at least 12 characters (Enter to make one): ")
    if typed and len(typed) < 12:
        w.say("  That's too short, so one's been made for you.")
        typed = ""
    password = typed or secrets.token_urlsafe(18)
    w.set_secrets({"DASHBOARD_TOKEN": password})
    if not typed:
        w.say(f"  The dashboard's password is: {password}\n  It's in {w.path.parent / '.env'} if you lose it.")


def _show_service(w: Wizard) -> str:
    if not shutil.which("systemctl"):
        return "needs Linux with systemd"
    return "installed" if (service.unit_dir() / "athena.service").exists() else "not installed"


def _change_service(w: Wizard) -> None:
    if sys.platform != "linux" or not shutil.which("systemctl"):
        w.say("  Athena runs as a service on Linux with systemd, such as Raspberry Pi OS.")
        return
    if not w.yes("Run Athena as a service, starting when this computer does? It uses sudo"):
        return
    service.ensure_bubblewrap(w.run)
    written = service.install(w.run, config=w.path)
    w.say(f"  Installed {', '.join(written)}." if written else "  The service was already up to date.")
    w.run(["sudo", "systemctl", "restart", "athena"], check=True)
    w.say("  Athena's running. `journalctl -u athena -f` shows what it's doing.")


PARTS = (
    Part("you", "You and Athena", _show_you, _change_you),
    Part("model", "The model", _show_model, _change_model),
    Part("memory", "Memory", _show_memory, _change_memory),
    Part("telegram", "Telegram", _show_telegram, _change_telegram),
    Part("dashboard", "The dashboard", _show_dashboard, _change_dashboard),
    Part("service", "Running Athena", _show_service, _change_service),
)


# -- helpers -------------------------------------------------------------------------------------------


def _unless(value: str, placeholder: str) -> str:
    return "" if value == placeholder else value


def _system_zone() -> str:
    try:
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        return "Europe/London"


def _models(w: Wizard, url: str, key: str, quiet: bool = False) -> list[str]:
    """The models a server lists. Empty, saying why unless ``quiet``, if it can't be asked."""
    try:
        response = w.http.get(f"{url}/models", headers={"Authorization": f"Bearer {key}"} if key else {})
        response.raise_for_status()
        return sorted(m["id"] for m in response.json().get("data", []))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        if not quiet:
            w.say(f"  Couldn't ask {url} for its models ({type(exc).__name__}). You can still type one.")
        return []


def _choose(w: Wizard, question: str, options: list[str], now: str) -> str:
    """One of ``options``, by number or by name, or one typed in."""
    for i, option in enumerate(options, 1):
        w.say(f"  {i}  {option}")
    answer = w.ask(f"{question}{' (number or name)' if options else ''}", now)
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    return answer


def _telegram(w: Wizard, token: str, method: str, **params: Any) -> Any:
    """A Telegram Bot API call's result, or None if it failed."""
    try:
        response = w.http.get(f"{TELEGRAM}/bot{token}/{method}", params=params, timeout=40)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body.get("result") if body.get("ok") else None


def _your_id(w: Wizard, token: str, bot: str) -> int | None:
    """Your Telegram user ID, from a message you send the bot now. None if none comes, or Athena's already
    answering the bot (Telegram gives its messages to one reader at a time)."""
    if not w.yes(f"Send @{bot} any message in Telegram, so it learns who you are. Ready to wait for it?"):
        return None
    pending = _telegram(w, token, "getUpdates", timeout=0)
    if pending is None:
        w.say("  Telegram won't give the bot's messages here, probably because Athena's running and reading them.")
        return None
    offset = max((u["update_id"] for u in pending), default=0) + 1  # only messages from now on
    deadline = time.monotonic() + WAIT_FOR_MESSAGE
    while time.monotonic() < deadline:
        updates = _telegram(w, token, "getUpdates", offset=offset, timeout=20) or []
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            sender = (update.get("message") or {}).get("from") or {}
            if sender.get("id"):
                _telegram(w, token, "getUpdates", offset=offset, timeout=0)  # so Athena doesn't answer it later
                w.say(f"  Got it: that's {sender.get('first_name', 'you')}.")
                return int(sender["id"])
    w.say("  No message came.")
    return None


def main(config: str | Path | None = None) -> int:
    try:
        Wizard(config=config).start()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped: what you'd saved is kept.")
        return 130
    return 0
