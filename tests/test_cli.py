"""The athena command: chatting in the terminal, adding notes to memory, starting Athena with its config brought
up to date first, and handing the service, update, Mac and embeddings commands to what does them."""

import asyncio
import builtins
import subprocess
from pathlib import Path

import pytest
from conftest import DIMS, FakeLLMServer, completion

from athena import cli, service
from athena.config import load_config
from athena.memory import Embedder, MemoryService, MemoryStore

CONFIG = """\
[llm]
base_url = "http://127.0.0.1:9/v1"
model = "test-model"
warm_up_minutes = 0

[embeddings]
base_url = "{url}"
model = "fake-a"
dimensions = {dims}

[display]
enabled = false

[dashboard]
enabled = false

[code]
enabled = false
"""


@pytest.fixture
def config(tmp_path, embeddings_server, monkeypatch):
    """A config.toml whose embeddings come from a fake server, and whose model is a fake one."""
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(url=embeddings_server.url, dims=DIMS))
    (tmp_path / ".env").write_text("")
    model = FakeLLMServer(lambda body: completion("Hello from the model."))
    monkeypatch.setattr("athena.app.LLMClient", lambda cfg: model.client(cfg))
    return path


def run(*argv) -> int:
    with pytest.raises(SystemExit) as exited:
        cli.main(list(argv))
    return exited.value.code


async def search(path: Path, query: str) -> str:
    cfg = load_config(path)
    store = MemoryStore(cfg.db_path, DIMS, cfg.embeddings.model)
    memory = MemoryService(store, Embedder(cfg.embeddings), cfg.memory)
    try:
        return (await memory.search(query, 1))[0].text
    finally:
        store.close()
        await memory.embedder.close()


def test_adding_notes_to_memory_and_embedding_everything_again(config, tmp_path, capsys):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "boiler.md").write_text("# The boiler\n\nThe boiler was serviced by Hamid in March.\n")
    (notes / "skip.bin").write_bytes(b"\x00\x01")
    assert run("-c", str(config), "ingest", str(notes)) == 0
    assert "boiler.md" in capsys.readouterr().out
    assert "Hamid" in asyncio.run(search(config, "who serviced the boiler"))
    assert run("-c", str(config), "reindex") == 0
    assert "Hamid" in asyncio.run(search(config, "who serviced the boiler"))


def test_chatting_in_the_terminal(config, monkeypatch, capsys):
    typed = iter(["hello", "/tools", "/session", "", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(typed))
    assert run("-c", str(config), "chat") == 0
    out = capsys.readouterr().out
    assert "athena> Hello from the model." in out.replace("Athena>", "athena>")
    assert "- remember [" in out  # /tools lists them
    assert "Started afresh: I won't see what we've said here so far." in out


def test_athena_starts_with_its_config_brought_up_to_date(config, monkeypatch):
    """A config.toml from an older Athena wouldn't load, so it's brought up to date first."""
    config.write_text(config.read_text().replace("[display]\n", "[display]\nled = true\n"))
    started = []
    monkeypatch.setattr("athena.telegram_bot.TelegramBot.run", lambda self: started.append(self))
    assert run("-c", str(config), "run") == 0
    assert started and "led" not in load_config(config).display.model_dump()
    assert "\nled = true" not in config.read_text()


def test_without_a_config_it_says_to_run_setup(tmp_path, capsys):
    assert run("-c", str(tmp_path / "nowhere.toml"), "doctor") == 2
    assert "run `athena setup` to make one" in capsys.readouterr().err


def test_the_service_and_update_commands(tmp_path, monkeypatch, capsys):
    done = []
    monkeypatch.setattr(service, "install", lambda config=None: done.append(("install", config)) or ["athena.service"])
    monkeypatch.setattr(service, "uninstall", lambda: done.append(("uninstall",)))
    monkeypatch.setattr(service, "update", lambda config=None: done.append(("update", config)))
    config = str(tmp_path / "config.toml")
    assert run("-c", config, "service", "install") == 0
    assert run("service", "uninstall") == 0
    assert run("-c", config, "update") == 0
    assert done == [("install", config), ("uninstall",), ("update", config)]
    out = capsys.readouterr().out
    assert "Installed athena.service." in out and "Updated and restarted Athena." in out


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, args, env=None, **kwargs):
        self.calls.append((args, {k: v for k, v in (env or {}).items() if k in ("ATHENA_SOURCE", "ATHENA")}))
        return subprocess.CompletedProcess(args, 0)


def test_the_mac_and_embeddings_commands_run_the_scripts_that_come_with_athena(monkeypatch):
    ran = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", ran)
    monkeypatch.setattr(cli, "_source", lambda: "git+https://github.com/someone/athena@abc123")
    monkeypatch.setattr(service, "program", lambda: Path("/home/pi/.local/bin/athena"))
    assert run("mac", "connect", "my-mac.ts.net", "george", "laptop") == 0
    assert run("mac", "install", "--pi-key", "ssh-ed25519 AAAA", "--documents", "--scope", "~/Documents") == 0
    assert run("embeddings", "switch", "embeddinggemma-2-8bit", "--on-mac") == 0
    assert run("logs", "-n", "20") == 0
    scripts = [(Path(args[1]).name, args[2:], env) for args, env in ran.calls[:3]]
    source = {"ATHENA_SOURCE": "git+https://github.com/someone/athena@abc123"}
    assert scripts == [
        ("connect-mac.sh", ["my-mac.ts.net", "george", "laptop"], source),  # the Mac's told the Pi's version
        ("mac-install.sh", ["--pi-key", "ssh-ed25519 AAAA", "--documents", "--scope", "~/Documents"], source),
        ("switch-embeddings.sh", ["embeddinggemma-2-8bit", "--on-mac"], {"ATHENA": "/home/pi/.local/bin/athena"}),
    ]
    assert all(Path(args[1]).exists() for args, _ in ran.calls[:3])
    assert ran.calls[3][0] == ["journalctl", "-u", "athena", "-f", "-n", "20"]


def test_a_checkout_isnt_offered_to_the_mac(monkeypatch):
    ran = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", ran)
    monkeypatch.setattr(cli, "_source", lambda: "file:///home/pi/athena")
    assert run("mac", "connect", "my-mac.ts.net", "george") == 0
    assert ran.calls[0][1] == {}  # so the printed command installs from GitHub, not a folder the Mac doesn't have


class Installed:
    def __init__(self, direct_url):
        self.direct_url = direct_url

    def read_text(self, name):
        return self.direct_url if name == "direct_url.json" else None


def test_where_athena_was_installed_from(monkeypatch):
    git = '{"url": "https://github.com/someone/athena", "vcs_info": {"vcs": "git", "commit_id": "abc123"}}'
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: Installed(git))
    assert cli._source() == "git+https://github.com/someone/athena@abc123"
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: Installed(None))
    assert cli._source() == Path(cli.__file__).resolve().parents[2].as_uri()  # this checkout
