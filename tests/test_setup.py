"""`athena setup` (setup_wizard.py), answered by a script: setting Athena up the first time, changing one part
later, and what it asks the model server and Telegram, which are stand-ins here."""

import os
import stat
import tomllib

import httpx
import pytest

from athena import setup_wizard
from athena.setup_wizard import Wizard

SECRETS = ("TELEGRAM_BOT_TOKEN", "LLM_API_KEY", "DASHBOARD_TOKEN")
TOKEN = "123456:a-bot-token"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".athena"
    monkeypatch.setenv("ATHENA_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))  # no old checkout to find
    monkeypatch.delenv("ATHENA_CONFIG", raising=False)
    monkeypatch.setattr(setup_wizard, "WAIT_FOR_MESSAGE", 1)
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    yield home
    for name in SECRETS:  # set by the code under test, not monkeypatch
        os.environ.pop(name, None)


def servers(updates=None):
    """The model server, listing its models, and Telegram, where the bot is @athena_test_bot and a message
    from you comes in once it's asked for new ones."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            if request.headers.get("authorization") != "Bearer sk-mac":
                return httpx.Response(401)
            ids = ["gemma-4-26b-a4b-it-4bit", "embeddinggemma-2-bf16", "qwen3.6-35b-a3b"]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if request.url.path == f"/bot{TOKEN}/getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "athena_test_bot"}})
        if request.url.path == f"/bot{TOKEN}/getUpdates":
            offset = int(request.url.params.get("offset", 0))
            pending = updates if updates is not None else [{"update_id": 7, "message": {"from": {"id": 1}}}]
            new = [{"update_id": 9, "message": {"from": {"id": 4242, "first_name": "George"}}}] if offset else []
            return httpx.Response(200, json={"ok": True, "result": [*pending, *new] if not offset else new})
        return httpx.Response(401, json={"ok": False})

    return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test"), seen


def wizard(answers, secrets, client):
    said = []
    answers, secrets = list(answers), list(secrets)
    w = Wizard(
        ask=lambda prompt: (said.append(prompt), answers.pop(0))[1],
        secret=lambda prompt: (said.append(prompt), secrets.pop(0))[1],
        say=said.append,
        http=client,
        run=lambda *a, **k: pytest.fail("nothing should be run"),
    )
    return w, said, answers, secrets


def test_setting_up_for_the_first_time(home, monkeypatch):
    monkeypatch.setattr(setup_wizard, "TELEGRAM", "http://test")
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")  # no service to install here
    client, seen = servers()
    answers = [
        "George", "", "Mars/Olympus", "Europe/London",  # you: a time zone that isn't one, then one that is
        "http://mac:8000/v1/", "3",  # the model: its server, then the third it lists
        "", "",  # memory: the model's server, and its embeddings model
        "y",  # ready to send the bot a message
    ]  # fmt: skip
    secrets = ["sk-mac", TOKEN, ""]  # the model's key, the bot's token, and a dashboard password made for you
    w, said, answers, secrets = wizard(answers, secrets, client)
    w.start()
    assert answers == [] and secrets == []

    config = tomllib.loads((home / "config.toml").read_text())
    assert config["agent"]["user_name"] == "George" and config["agent"]["timezone"] == "Europe/London"
    assert config["llm"] == {**config["llm"], "base_url": "http://mac:8000/v1", "model": "qwen3.6-35b-a3b"}
    assert config["embeddings"]["model"] == "embeddinggemma-2-bf16"  # as it was, which the server has
    assert config["telegram"]["allowed_user_ids"] == [4242]  # from your message, not one from before
    env = (home / ".env").read_text()
    assert f"TELEGRAM_BOT_TOKEN={TOKEN}" in env and "LLM_API_KEY=sk-mac" in env
    password = os.environ["DASHBOARD_TOKEN"]
    assert len(password) >= 20 and f"DASHBOARD_TOKEN={password}" in env
    assert any(password in line for line in said)  # shown once, so you can sign in
    assert TOKEN not in "".join(said) and "sk-mac" not in "".join(said)  # secrets never are
    for path, mode in ((home, 0o700), (home / "config.toml", 0o600), (home / ".env", 0o600)):
        assert stat.S_IMODE(path.stat().st_mode) == mode, path
    assert any("That isn't a time zone" in line for line in said)
    assert any("@athena_test_bot" in line for line in said)
    # The message you sent to say who you are is marked read, so Athena doesn't answer it later.
    asked = [r.url.params.get("offset") for r in seen if r.url.path.endswith("getUpdates")]
    assert asked[0] is None and asked[-1] == "10"


def test_changing_one_part_later(home, monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")
    client, _ = servers()
    w, *_ = wizard(["George", "", "Europe/London", "http://mac:8000/v1", "", "", "n"], ["sk-mac", "", ""], client)
    w.start()  # the first time, done quickly
    w, said, answers, _ = wizard(["1", "Georges", "Pallas", "", ""], [], client)
    w.start()  # change "You and Athena", keeping the time zone, then done
    assert answers == []
    config = tomllib.loads((home / "config.toml").read_text())["agent"]
    assert (config["user_name"], config["assistant_name"], config["timezone"]) == ("Georges", "Pallas", "Europe/London")
    menu = [line for line in said if line.startswith("  1  ")]
    assert menu and "George · Athena · Europe/London" in menu[0]


def test_a_secret_pasted_with_a_stray_character_is_asked_for_again(home, monkeypatch):
    """Such as the █ of a terminal's cursor, copied with a password. Memory on the model's own server is left
    empty, so it uses the model's key."""
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")
    client, _ = servers()
    w, *_ = wizard(["George", "", "Europe/London", "http://mac:8000/v1", "", "", "n"], ["sk-mac", "", ""], client)
    w.start()
    chosen = "open-sesame-123456"
    answers = ["3", "http://mac:8000/v1/", "", "5", "y", ""]  # memory, on the model's server; a new password
    w, said, answers, secrets = wizard(answers, [chosen + "\u2588", chosen], client)
    w.start()
    assert answers == [] and secrets == []
    assert any("That has a '\u2588' in it" in line for line in said)
    assert f"DASHBOARD_TOKEN={chosen}\n" in (home / ".env").read_text()
    assert tomllib.loads((home / "config.toml").read_text())["embeddings"]["base_url"] == ""


def test_a_model_server_that_cant_be_asked_still_lets_you_type_a_model(home, monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")
    client, _ = servers()
    answers = ["George", "", "Europe/London", "http://mac:8000/v1", "my-model", "", "", "n"]
    w, said, _, _ = wizard(answers, ["wrong-key", "", ""], client)
    w.start()
    assert tomllib.loads((home / "config.toml").read_text())["llm"]["model"] == "my-model"
    assert any("Couldn't ask http://mac:8000/v1 for its models" in line for line in said)


def test_when_athena_is_reading_the_bots_messages_you_give_your_id(home, monkeypatch):
    monkeypatch.setattr(setup_wizard, "TELEGRAM", "http://test")
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")
    usual, _ = servers()

    def busy(request):  # Telegram gives a bot's messages to one reader: Athena, here
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(409, json={"ok": False, "description": "Conflict"})
        return usual._transport.handle_request(request)

    client = httpx.Client(transport=httpx.MockTransport(busy), base_url="http://test")
    answers = ["George", "", "Europe/London", "http://mac:8000/v1", "", "", "", "y", "4242"]
    w, said, _, _ = wizard(answers, ["sk-mac", TOKEN, ""], client)
    w.start()
    assert tomllib.loads((home / "config.toml").read_text())["telegram"]["allowed_user_ids"] == [4242]
    assert any("probably because Athena's running" in line for line in said)


def test_a_config_from_an_older_athena_is_brought_up_to_date_first(home, monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")
    client, _ = servers()
    wizard(["George", "", "Europe/London", "http://mac:8000/v1", "", "", "n"], ["sk-mac", "", ""], client)[0].start()
    config = home / "config.toml"
    config.write_text(config.read_text().replace("[display]\n", "[display]\nled = true\n", 1))
    w, said, _, _ = wizard([""], [], client)
    w.start()
    assert "config.toml: removed display.led" in said and "\nled = true" not in config.read_text()


def test_on_linux_it_installs_and_starts_the_service(home, monkeypatch, tmp_path):
    from test_service import Recorder

    from athena import service

    monkeypatch.setattr(setup_wizard.sys, "platform", "linux")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")  # bubblewrap's there
    monkeypatch.setattr(service, "program", lambda: tmp_path / "bin" / "athena")
    monkeypatch.setenv("ATHENA_UNIT_DIR", str(tmp_path))
    client, _ = servers()
    recorder = Recorder()
    answers = ["George", "", "Europe/London", "http://mac:8000/v1", "", "", "", "y"]  # ...and yes, run it
    w, said, answers, _ = wizard(answers, ["sk-mac", "", ""], client)
    w.run = recorder
    w.start()
    assert answers == []
    assert recorder.calls == [
        f"sudo tee {tmp_path}/athena.service",
        f"sudo tee {tmp_path}/athena-stopped.service",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable athena",
        "sudo systemctl restart athena",
    ]
    assert f"WorkingDirectory={home}" in (tmp_path / "athena.service").read_text()
    assert any("Athena's running" in line for line in said)


def test_changes_are_used_once_the_running_athena_is_restarted(home, monkeypatch):
    """It reads them when it starts, so a new dashboard password does nothing until then."""
    from test_service import Recorder

    client, _ = servers()
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")
    wizard(["George", "", "Europe/London", "http://mac:8000/v1", "", "", "n"], ["sk-mac", "", ""], client)[0].start()
    monkeypatch.setattr(setup_wizard.sys, "platform", "linux")
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: f"/usr/bin/{name}")

    def setup(answers, secrets=()):
        w, said, answers, _ = wizard(answers, secrets, client)
        w.run = Recorder()
        w.start()
        assert answers == []
        return w.run.calls, said

    calls, _ = setup(["5", "y", "", "y"], ["open-sesame-123456"])  # a new password, then yes, restart
    assert calls == ["systemctl is-active --quiet athena", "sudo systemctl restart athena"]
    calls, said = setup(["5", "y", "", "n"], ["open-sesame-654321"])
    assert calls == ["systemctl is-active --quiet athena"]
    assert "  It'll use them when it next starts: `sudo systemctl restart athena`." in said
    calls, _ = setup(["5", "n", ""])  # nothing changed: nothing to ask
    assert calls == []
