"""The dashboard, served for real on a free local port by Athena's own services."""

import asyncio
import gzip
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import FakeEmbedder, FakeLLMServer, completion

from pi_assistant.app import build_services
from pi_assistant.config import load_config
from pi_assistant.dashboard import COOKIE, FILES, SIGN_IN_TRIES
from pi_assistant.doctor import FAIL, OK, WARN, check_dashboard
from pi_assistant.tool_approvals import ToolVersion

PASSWORD = "dashboard-password-123"
DEMO = Path(__file__).parent / "fixtures" / "demo_mcp_server.py"
CONFIG = f"""
[llm]
base_url = "http://127.0.0.1:9/v1"
model = "test-model"
warm_up_minutes = 0

[agent]
assistant_name = "Athena"
user_name = "George"
timezone = "Europe/London"

[embeddings]
model = "fake-embed"
dimensions = 256

[display]
enabled = false

[dashboard]
port = 0

[code]
enabled = false  # it's there only on Linux: off, so these tests count the same tools everywhere

[news.feeds]
"BBC News" = "https://feeds.bbci.co.uk/news/rss.xml"

[mcp_servers.demo]
command = "{sys.executable}"
args = ["{DEMO}"]
include = ["add", "get_env"]
confirm = []
"""


def reply(body: dict) -> dict:
    """The model: adds numbers with the demo server's tool when asked to, otherwise just answers."""
    last = body["messages"][-1]
    if last["role"] == "user" and "add" in last["content"] and body.get("tools"):
        return completion(None, [("add", {"a": 2, "b": 40})])
    return completion("All done.")


@pytest.fixture
async def athena(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", PASSWORD)
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / ".env").write_text(f"DASHBOARD_TOKEN={PASSWORD}\n")
    cfg = load_config(tmp_path / "config.toml")
    services = build_services(cfg)
    model = FakeLLMServer(
        reply, model={"max_model_len": 262144}, status={"models": [{"id": "test-model", "max_context_window": 131072}]}
    )
    services.llm = services.agent.llm = model.client(cfg.llm)
    services.memory.embedder = FakeEmbedder()
    services.mcp.sandbox = None  # the demo server lives in the repo; the sandbox has tests of its own
    await services.start()
    try:
        yield services
    finally:
        await services.close()


def client(services, signed_in=True, **kwargs) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {PASSWORD}"} if signed_in else {}
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{services.dashboard.port}", headers=headers, timeout=10, **kwargs
    )


async def until(condition, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


# -- the page and signing in ------------------------------------------------------------------------


async def test_the_page_is_public_and_everything_else_needs_the_password(athena):
    async with client(athena, signed_in=False) as http:
        page = await http.get("/")
        assert page.status_code == 200 and "dashboard.js" in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"
        for path in FILES:
            assert (await http.get(path)).status_code == 200, path
        response = await http.get("/api/overview")
        assert response.status_code == 401 and response.json() == {"error": "Sign in first."}
        assert (await http.get("/api/overview", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await http.get("/health")).text == "ok"
        assert (await http.get("/nothing")).status_code == 404
    async with client(athena) as http:
        assert (await http.get("/api/overview")).status_code == 200


async def test_signing_in_sets_a_cookie_for_90_days(athena):
    async with client(athena, signed_in=False) as http:
        wrong = await http.post("/api/login", json={"password": "nope"})
        assert wrong.status_code == 401 and "cookie" not in wrong.headers.get("set-cookie", "")
        right = await http.post("/api/login", json={"password": PASSWORD})
        cookie = right.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert "Max-Age=7776000" in cookie
        assert (await http.get("/api/overview")).status_code == 200  # httpx keeps the cookie

        value = http.cookies[COOKIE]
        expires, nonce, signature = value.split(".")
        for forged in [f"{expires}.{nonce}.{'0' * 64}", f"{int(expires) + 1}.{nonce}.{signature}", "x", ""]:
            http.cookies.set(COOKIE, forged)
            assert (await http.get("/api/overview")).status_code == 401, forged

        out = await http.post("/api/logout", json={})
        assert "Max-Age=0" in out.headers["set-cookie"]


async def test_an_old_cookie_stops_working(athena, monkeypatch):
    dashboard = athena.dashboard
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    old = dashboard._cookie()  # made 90 days before the real now
    monkeypatch.undo()
    async with client(athena, signed_in=False, cookies={COOKIE: old}) as http:
        assert (await http.get("/api/overview")).status_code == 401


async def test_changing_the_password_signs_everyone_out(athena):
    async with client(athena, signed_in=False) as http:
        await http.post("/api/login", json={"password": PASSWORD})
        athena.dashboard.cfg.token = "a-brand-new-password"
        assert (await http.get("/api/overview")).status_code == 401


async def test_wrong_passwords_are_limited(athena):
    async with client(athena, signed_in=False) as http:
        for _ in range(SIGN_IN_TRIES):
            assert (await http.post("/api/login", json={"password": "guess"})).status_code == 401
        assert (await http.post("/api/login", json={"password": PASSWORD})).status_code == 429


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Type": "application/x-www-form-urlencoded"}, 415),  # what a form on another site sends
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "application/json", "Sec-Fetch-Site": "same-site"}, 403),
        ({"Content-Type": "application/json", "Origin": "https://evil.example"}, 403),
        ({"Content-Type": "application/json", "Origin": "null"}, 403),
        ({"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}, 200),
        ({"Content-Type": "application/json"}, 200),  # curl, with the password
    ],
)
async def test_changes_only_come_from_the_dashboards_own_page(athena, headers, status):
    async with client(athena) as http:
        response = await http.post("/api/session", content=b"{}", headers=headers)
        assert response.status_code == status, response.text


async def test_the_page_from_its_own_address_can_make_changes(athena):
    async with client(athena) as http:
        host = f"127.0.0.1:{athena.dashboard.port}"
        response = await http.post("/api/session", json={}, headers={"Origin": f"http://{host}"})
        assert response.status_code == 200
        behind_tailscale = {"Origin": "https://pi.tail1234.ts.net", "X-Forwarded-Host": "pi.tail1234.ts.net"}
        assert (await http.post("/api/session", json={}, headers=behind_tailscale)).status_code == 200


async def test_files_are_cached_and_compressed(athena):
    async with client(athena, signed_in=False) as http:
        first = await http.get("/dashboard.js", headers={"Accept-Encoding": "gzip"})
        assert first.headers["content-encoding"] == "gzip" and first.headers["cache-control"] == "no-cache"
        assert first.text.startswith("// Athena's dashboard")  # httpx unzips it
        raw = await http.get("/dashboard.js", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in raw.headers and raw.content == first.content
        again = await http.get("/dashboard.js", headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
        assert len(gzip.compress(raw.content)) < len(raw.content) / 2


# -- the Status tab -------------------------------------------------------------------------------------


async def test_the_overview_counts_messages_tools_and_context(athena):
    await athena.agent.respond("chat", "Please add 2 and 40", channel="Telegram")
    await athena.agent.respond("chat", "Thanks", channel="Siri")
    async with client(athena) as http:
        await until(lambda: athena.dashboard._model["checked"])
        overview = (await http.get("/api/overview")).json()
    assert overview["assistant"] == {"name": "Athena", "user": "George", "timezone": "Europe/London"}
    session = overview["session"]
    assert (session["queries"], session["tool_calls"], session["failed"]) == (2, 1, 0)
    assert session["tools"] == [{"name": "add", "calls": 1, "failed": 0, "declined": 0}]
    assert session["context"]["tokens"] == 120  # what the fake model said it read and wrote, last time
    assert overview["total"]["queries"] == 2 and overview["total"]["sessions"] == 1
    assert overview["model"] | {"checked": 0} == {
        "name": "test-model",
        "thinking": "on",
        "short_thinking_tokens": 1000,
        "reachable": True,
        "context_window": 131072,  # what oMLX is set to, below what the model can do
        "error": None,
        "checked": 0,
    }
    # With the tools for tasks and watching, files, and the MCP Registry's.
    assert overview["tools"] == {"count": 18, "servers": 1, "connected": 1, "applying": False}
    assert overview["status"]["state"] == "idle" and overview["status"]["last_task"] == "Thanks"


async def test_a_new_session_and_forgetting_everything(athena):
    await athena.agent.respond("chat", "My sister is called Anna")
    await until(lambda: athena.memory.store.count().get("conversation"))
    await athena.memory.remember("George's sister is called Anna.")
    async with client(athena) as http:
        session = (await http.post("/api/session", json={})).json()["session"]
        assert (session["id"], session["queries"], session["context"]) == (2, 0, None)
        assert athena.history.load("chat") == []

        refused = await http.post("/api/forget", json={})
        assert refused.status_code == 400 and athena.memory.store.count() == {"fact": 1, "conversation": 1}
        after = (await http.post("/api/forget", json={"confirm": "forget"})).json()
    assert after["memory"] == {"facts": 0, "documents": 0, "conversations": 0}
    assert after["session"]["id"] == 3 and after["total"]["queries"] == 1  # the statistics stay


async def test_switching_thinking_on_short_or_off(athena):
    model = FakeLLMServer(lambda body: completion("Hi."))
    athena.llm = athena.agent.llm = model.client(athena.config.llm)
    async with client(athena) as http:
        overview = (await http.get("/api/overview")).json()
        assert (overview["model"]["thinking"], overview["model"]["short_thinking_tokens"]) == ("on", 1000)

        overview = (await http.post("/api/thinking", json={"mode": "off"})).json()
        assert overview["model"]["thinking"] == "off"
        await athena.agent.respond("1", "hi")
        assert model.requests[-1]["chat_template_kwargs"] == {"enable_thinking": False}

        await http.post("/api/thinking", json={"mode": "short"})
        await athena.agent.respond("1", "hi again")
        assert model.requests[-1]["chat_template_kwargs"] == {"enable_thinking": True}
        assert model.requests[-1]["thinking_budget"] == 1000
        assert load_config(athena.config.path).llm.thinking == "short"  # kept for after a restart

        response = await http.post("/api/thinking", json={"mode": "lots"})
        assert response.status_code == 400 and response.json() == {
            "error": "Thinking can be on, short, off, not 'lots'."
        }
        assert athena.config.llm.thinking == "short"


async def test_tasks_from_the_dashboard(athena):
    said = []

    class Telegram:
        chat_id = "111"

        async def tell(self, text):
            said.append(text)

        async def ask(self, tool, args, summary, *, why, timeout):
            return False

    athena.channel = Telegram()
    async with client(athena) as http:
        listing = (await http.get("/api/tasks")).json()
        assert listing["tasks"] == [] and listing["tools"] == ["forget_memory"]  # the tools that ask first
        examples = [e["title"] for e in listing["examples"]]
        assert examples == ["Morning briefing", "MCP servers worth adding", "Skills from habits", "Research"]
        assert listing["quiet_minutes"] == 5 and "30 7 * * 1-5" in listing["schedules"]

        bad = await http.post("/api/tasks", json={"title": "X", "instructions": "Y", "schedule": "sometime"})
        assert bad.status_code == 400 and "I don't understand the schedule" in bad.json()["error"]
        bad = await http.post("/api/tasks", json={"title": "X", "instructions": "Y", "schedule": "now", "tools": "add"})
        assert bad.status_code == 400

        # You haven't been chatting, so it runs straight away, and tells you what it found.
        new = {"title": "Sums", "instructions": "Work something out.", "schedule": "now", "tools": []}
        listing = (await http.post("/api/tasks", json=new)).json()
        [task] = listing["tasks"]
        assert (task["title"], task["created_by"], task["when"]) == (
            "Sums",
            "you",
            "once, as soon as you're not chatting",
        )
        await until(lambda: said)
        assert said == ["📋 **Sums**\n\nAll done."]
        await until(lambda: athena.tasks.store.get(task["id"]).status == "done")
        [task] = (await http.get("/api/tasks")).json()["tasks"]
        assert (task["status"], task["last_outcome"], task["last_result"]) == ("done", "told you", "All done.")

        regular = {"title": "Daily", "instructions": "Check.", "schedule": "30 7 * * *", "tools": []}
        listing = (await http.post("/api/tasks", json=regular)).json()
        daily = listing["tasks"][1]
        assert daily["status"] == "active" and daily["next_run"] > time.time()
        paused = (await http.post(f"/api/tasks/{daily['id']}", json={"action": "pause"})).json()["tasks"][1]
        assert paused["status"] == "paused" and paused["next_run"] is None
        resumed = (await http.post(f"/api/tasks/{daily['id']}", json={"action": "resume"})).json()["tasks"][1]
        assert resumed["status"] == "active"
        assert (await http.post(f"/api/tasks/{daily['id']}", json={"action": "explode"})).status_code == 400
        assert (await http.post("/api/tasks/nope", json={"action": "run"})).status_code == 404
        assert (await http.delete(f"/api/tasks/{daily['id']}")).status_code == 200
        assert [t["id"] for t in (await http.get("/api/tasks")).json()["tasks"]] == [task["id"]]
        assert (await http.delete(f"/api/tasks/{daily['id']}")).status_code == 400  # already gone


async def test_the_status_waits_for_a_change(athena):
    async with client(athena) as http:
        first = (await http.get("/api/status")).json()
        assert first["status"]["state"] == "idle"
        asyncio.get_running_loop().call_later(0.2, lambda: athena.status.begin("Something new"))
        started = time.monotonic()
        changed = (await http.get(f"/api/status?wait=10&after={first['version']}")).json()
        assert time.monotonic() - started < 5
        assert changed["status"]["state"] == "working" and changed["version"] != first["version"]
        assert (await http.get("/api/status?wait=soon")).status_code == 400


# -- the Tools tab ----------------------------------------------------------------------------------------


async def wait_applied(http) -> dict:
    for _ in range(200):
        tools = (await http.get("/api/tools")).json()
        if not tools["applying"]:
            return tools
        await asyncio.sleep(0.05)
    raise AssertionError("still applying")


def tool(tools: dict, name: str) -> dict:
    return next(t for t in tools["tools"] if t["id"] == name)


async def test_switching_a_server_off_and_on_again(athena):
    async with client(athena) as http:
        tools = (await http.get("/api/tools")).json()
        assert tools["editable"] and not tools["applying"]
        assert tool(tools, "demo")["detail"] == "Connected · 2 tools"

        response = await http.post("/api/tools/demo", json={"enabled": False})
        assert response.status_code == 200
        tools = await wait_applied(http)
        assert tool(tools, "demo")["state"] == "off"
        assert not [t for t in athena.tools.all() if t.source == "mcp:demo"]

        await http.post("/api/tools/demo", json={"enabled": True})
        tools = await wait_applied(http)
        assert tool(tools, "demo")["state"] == "on"
        assert {t.name for t in athena.tools.all() if t.source == "mcp:demo"} == {"add", "get_env"}
    assert "enabled = true" in athena.config.path.read_text()


async def test_choosing_a_servers_tools(athena):
    async with client(athena) as http:
        demo = tool((await http.get("/api/tools")).json(), "demo")
        offered = [t["name"] for t in demo["tools"]]
        assert set(offered) == {"add", "delete_note", "hidden_tool", "explode", "get_env"}
        choice = {name: {"on": name in ("add", "explode"), "asks": name == "explode"} for name in offered}
        assert (await http.post("/api/tools/demo", json={"tools": choice})).status_code == 200
        await wait_applied(http)
    demo_tools = {t.name: t for t in athena.tools.all() if t.source == "mcp:demo"}
    assert set(demo_tools) == {"add", "explode"}
    assert not demo_tools["add"].needs_confirmation and demo_tools["explode"].needs_confirmation


async def test_adding_and_removing_a_server(athena):
    async with client(athena) as http:
        added = await http.post(
            "/api/tools",
            json={"name": "second", "command": sys.executable, "args": [str(DEMO)], "env": {"KEY": {"secret": "s-1"}}},
        )
        assert added.status_code == 200
        tools = await wait_applied(http)
        assert tool(tools, "second")["state"] == "on" and tool(tools, "second")["custom"]
        get_env = athena.tools.get("second__get_env")  # its tool names are taken by the first server
        assert get_env and get_env.needs_confirmation  # a new server's tools ask first

        refused = await http.post("/api/tools", json={"name": "second", "command": "uvx"})
        assert refused.status_code == 400 and "already" in refused.json()["error"]

        assert (await http.delete("/api/tools/second", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await http.delete("/api/tools/second")).status_code == 200
        tools = await wait_applied(http)
        assert "second" not in [t["id"] for t in tools["tools"]]
        assert athena.tools.get("second__get_env") is None
        assert not athena.mcp.approvals.knows("second")  # one added later with its name starts afresh


async def test_changed_tools_wait_for_your_approval(athena):
    # As if `add` said something else when you approved it.
    before = ToolVersion("Add two numbers.", {"type": "object", "properties": {}})
    athena.mcp.approvals.approve("demo", {"add": before})
    await athena.mcp.reload({"demo"})
    assert athena.tools.get("add") is None  # held back from the model
    async with client(athena) as http:
        demo = tool((await http.get("/api/tools")).json(), "demo")
        assert (demo["state"], demo["detail"]) == ("attention", "1 tool changed: waiting for you")
        [held] = demo["held"]
        assert held["name"] == "add" and held["approved"] == {
            "description": before.description,
            "parameters": before.parameters,
        }
        assert held["now"]["parameters"]["properties"]["a"]["type"] == "integer"

        stale = await http.post("/api/tools/demo/approve", json={"tools": {"add": "an-older-version"}})
        assert stale.status_code == 409 and "have another look" in stale.json()["error"]
        assert (await http.post("/api/tools/demo/approve", json={"tools": ["add"]})).status_code == 400
        assert athena.tools.get("add") is None

        athena.rewarm.clear()
        approved = await http.post("/api/tools/demo/approve", json={"tools": {"add": held["fingerprint"]}})
        assert approved.status_code == 200
        demo = tool(approved.json(), "demo")
        assert demo["state"] == "on" and demo["held"] == []
    assert athena.tools.get("add") is not None and athena.rewarm.is_set()  # the prompt has changed


async def test_changes_that_cant_be_made_say_why(athena):
    async with client(athena) as http:
        response = await http.post("/api/tools/github", json={"enabled": True})
        assert response.status_code == 400 and response.json() == {"error": "Fill in “GitHub token” first."}
        assert (
            await http.post("/api/tools/github", content=b"[1]", headers={"Content-Type": "application/json"})
        ).status_code == 400
        assert (await http.put("/api/tools/github", json={})).status_code == 405


async def test_reconnecting_reads_the_config_again(athena):
    path = athena.config.path
    path.write_text(
        path.read_text().replace("[news.feeds]\n", '[news.feeds]\n"Hacker News" = "https://hnrss.org/frontpage"\n')
    )
    async with client(athena) as http:
        assert (await http.post("/api/reconnect", json={})).status_code == 200
        await wait_applied(http)
    assert set(athena.config.news.feeds) == {"BBC News", "Hacker News"}


async def test_messages_wait_while_tools_change(athena):
    gate = asyncio.Event()

    async def slow(args):
        await gate.wait()
        return "slow result"

    from pi_assistant.tools import Tool

    athena.builtins.tools.append(Tool("slow", "Takes a while.", {"type": "object", "properties": {}}, slow))
    athena.agent.llm = FakeLLMServer([completion(None, [("slow", {})]), completion("Done slowly.")]).client(
        athena.config.llm
    )
    answering = asyncio.create_task(athena.agent.respond("chat", "do the slow thing"))
    await until(lambda: athena.agent.busy)
    async with client(athena) as http:
        await http.post("/api/tools/demo", json={"enabled": False})
        await asyncio.sleep(0.3)
        assert (await http.get("/api/tools")).json()["applying"]  # waiting for the answer to finish
        assert athena.tools.get("add")  # the tools haven't changed under it
        gate.set()
        assert (await answering).text == "Done slowly."
        await wait_applied(http)
    assert athena.tools.get("add") is None


# -- starting -----------------------------------------------------------------------------------------------


async def test_athena_makes_a_password_if_there_isnt_one(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "")
    (tmp_path / "config.toml").write_text(CONFIG.split("[mcp_servers.demo]")[0])
    (tmp_path / ".env").write_text("# Secrets\nDASHBOARD_TOKEN=\n")
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.dashboard.token == ""
    services = build_services(cfg)
    services.memory.embedder = FakeEmbedder()
    try:
        await services.start()
        token = services.dashboard.cfg.token
        assert len(token) >= 20 and f"DASHBOARD_TOKEN={token}" in (tmp_path / ".env").read_text()
        reported = []
        cfg.dashboard.port = services.dashboard.port
        await check_dashboard(cfg, lambda mark, msg: reported.append((mark, msg)))
        assert reported == [
            (OK, f"answering at http://127.0.0.1:{cfg.dashboard.port}. Sign in with DASHBOARD_TOKEN from .env")
        ]
    finally:
        await services.close()


async def test_it_wont_start_with_a_short_password(config, caplog):
    config.dashboard.token = "short"
    config.dashboard.port = 0
    services = build_services(config)
    try:
        assert not await services.dashboard.start()
        assert "at least 12 characters" in caplog.text
    finally:
        await services.close()


async def test_doctor_explains_what_is_wrong(config):
    reported = []

    def report(mark, msg):
        reported.append((mark, msg))

    config.dashboard.token = "short"
    await check_dashboard(config, report)
    config.dashboard.token = ""
    config.dashboard.port = 9  # nothing listens there
    await check_dashboard(config, report)
    config.dashboard.enabled = False
    await check_dashboard(config, report)
    assert reported == [
        (FAIL, "its password, DASHBOARD_TOKEN in .env, must be at least 12 characters"),
        (WARN, "no password yet: Athena makes one, DASHBOARD_TOKEN in .env, when the service starts"),
        (WARN, "not answering. It runs inside the bot: is the pi-assistant service running?"),
        (WARN, "off ([dashboard] enabled = false)"),
    ]


async def test_an_unexpected_failure_is_a_500_not_a_dropped_connection(athena, monkeypatch, caplog):
    def broken():
        raise RuntimeError("the database is on fire")

    monkeypatch.setattr(athena.dashboard, "overview", broken)
    async with client(athena) as http:
        response = await http.get("/api/overview")
    assert response.status_code == 500 and response.json() == {"error": "Something went wrong. Athena's log says what."}
    assert "on fire" not in response.text and "on fire" in caplog.text


# -- the Memory tab: search, and documents from your Mac ----------------------------------------------


@pytest.fixture
def your_mac(tmp_path, monkeypatch):
    """Your Mac's home folder, whose documents server runs here, as Athena on the Pi would start it."""
    import os

    home = Path(os.path.realpath(tmp_path)) / "mac"
    (home / "Documents" / "Taxes").mkdir(parents=True)
    (home / "Documents" / "Taxes" / "council.txt").write_text("Council tax band D for the flat on Elm Street.\n")
    (home / "Documents" / "boiler.md").write_text("The boiler service is due in March.\n")
    monkeypatch.setenv("HOME", str(home))
    return home


def point_at(athena, home):
    """Have Athena start the documents server here, in config.toml as you'd set it for SSH."""
    args = ["-m", "pi_assistant.mac_documents", "--scope", str(home)]
    athena.config = athena.dashboard.settings.set_values({"documents.command": sys.executable, "documents.args": args})
    athena.documents.configure(athena.config.documents)


async def test_choosing_folders_to_index(athena, your_mac):
    point_at(athena, your_mac)
    async with client(athena) as http:
        memory = (await http.get("/api/memory")).json()
        assert memory["documents"]["folders"] == [] and memory["editable"]
        assert (await http.post("/api/documents/index", json={})).json()["error"] == "Choose a folder to index first."

        start = (await http.get("/api/documents/browse")).json()
        assert {"name": "Documents", "path": "~/Documents"} in start["folders"]
        inside = (await http.get("/api/documents/browse", params={"path": "~/Documents"})).json()
        assert inside == {
            "path": "~/Documents",
            "folders": [{"name": "Taxes", "path": "~/Documents/Taxes"}],
            "documents": 1,
        }

        added = await http.post("/api/documents/folders", json={"add": "~/Documents/"})
        assert added.status_code == 200
        assert added.json()["documents"]["folders"][0]["folder"] == "~/Documents"
        for bad in [{"add": "Documents"}, {"add": "~/Documents"}, {"add": "~/a\nb"}, {"remove": "~/Desktop"}, {}]:
            response = await http.post("/api/documents/folders", json=bad)
            assert response.status_code == 400, bad
    assert 'folders = ["~/Documents"]' in athena.config.path.read_text()  # saved in config.toml


async def test_indexing_and_searching_from_the_dashboard(athena, your_mac):
    point_at(athena, your_mac)
    async with client(athena) as http:
        await http.post("/api/documents/folders", json={"add": "~/Documents"})
        assert (await http.post("/api/documents/stop", json={})).status_code == 200  # nothing to stop
        started = (await http.post("/api/documents/index", json={})).json()
        assert started["documents"]["running"]
        await until(lambda: not athena.documents.running)
        memory = (await http.get("/api/memory")).json()["documents"]
        assert memory["run"]["state"] == "done", memory["run"]["error"]
        assert memory["folders"] == [{"folder": "~/Documents", "files": 2, "passages": 2, "no_text": 0}]
        assert memory["mac"] and memory["indexed"]

        found = (await http.post("/api/memory/search", json={"query": "council tax band", "kind": "document"})).json()
        top = found["results"][0]
        assert (top["kind"], top["path"]) == ("document", "~/Documents/Taxes/council.txt")
        assert top["text"].startswith("Council tax band D")
        everything = (await http.post("/api/memory/search", json={"query": "boiler service March"})).json()
        assert everything["results"][0]["path"] == "~/Documents/boiler.md"
        for bad in [{"query": ""}, {"query": "x" * 501}, {"query": "x", "kind": "files"}]:
            assert (await http.post("/api/memory/search", json=bad)).status_code == 400

        overview = (await http.get("/api/overview")).json()
        assert overview["memory"]["documents"] == 2  # documents, not their passages

        # Removing the folder takes its documents out of memory straight away.
        removed = (await http.post("/api/documents/folders", json={"remove": "~/Documents"})).json()
        assert removed["documents"]["folders"] == [] and athena.memory.store.document_sources() == []


async def test_a_mac_that_cant_be_reached(athena):
    athena.config.documents.command = sys.executable
    athena.config.documents.args = [
        "-c",
        "import sys; print('ssh: Could not resolve hostname athena-laptop', file=sys.stderr); sys.exit(255)",
    ]
    athena.documents.configure(athena.config.documents)
    async with client(athena) as http:
        response = await http.get("/api/documents/browse")
    assert response.status_code == 502
    assert (
        "Couldn't reach your Mac" in response.json()["error"]
        and "Could not resolve hostname" in response.json()["error"]
    )


# -- reviewing what Athena suggested, and skills ------------------------------------------------------


async def test_suggested_memories_are_saved_changed_or_discarded(athena):
    store = athena.memory.store
    first = store.suggest("George's dentist is on Elm Street.", "your task “Errands”")
    second = store.suggest("George dislikes olives.", "your task “Recipes”")
    async with client(athena) as http:
        overview = (await http.get("/api/overview")).json()
        assert overview["review"] == {"memories": 2, "skills": 0}
        memory = (await http.get("/api/memory")).json()
        assert [(s["id"], s["source"]) for s in memory["suggestions"]] == [
            (first, "your task “Errands”"),
            (second, "your task “Recipes”"),
        ]

        changed = {"action": "approve", "text": "George's dentist is on Elm Street, Bath."}
        memory = (await http.post(f"/api/memory/suggestions/{first}", json=changed)).json()
        assert [s["id"] for s in memory["suggestions"]] == [second] and memory["facts"] == 1
        memory = (await http.post(f"/api/memory/suggestions/{second}", json={"action": "discard"})).json()
        assert memory["suggestions"] == [] and memory["facts"] == 1
        assert [h.text for h in store.recent()] == ["George's dentist is on Elm Street, Bath."]

        for path, body, status in [
            (f"/api/memory/suggestions/{first}", {"action": "approve"}, 404),  # saved already
            (f"/api/memory/suggestions/{second}", {"action": "discard"}, 404),
            ("/api/memory/suggestions/x", {"action": "discard"}, 404),
            (f"/api/memory/suggestions/{first}", {"action": "keep"}, 400),
            (f"/api/memory/suggestions/{first}", {"action": "approve", "text": " "}, 400),
        ]:
            assert (await http.post(path, json=body)).status_code == status, (path, body)


async def test_skills_from_the_dashboard(athena):
    trip = {"name": "Trip plans", "description": "When I ask for a trip", "body": "Trains first."}
    async with client(athena) as http:
        listing = (await http.get("/api/skills")).json()
        assert listing == {"skills": [], "suggestions": [], "reviews": True}

        listing = (await http.post("/api/skills", json=trip)).json()
        (skill,) = listing["skills"]
        assert (skill["name"], skill["created_by"], skill["versions"]) == ("Trip plans", "you", 1)
        assert athena.agent.prompt().endswith("## Your skills\n- Trip plans: When I ask for a trip")
        assert athena.rewarm.is_set()  # the prompt changed, so the model server reads it again

        edited = (await http.post(f"/api/skills/{skill['id']}", json={**trip, "body": "Trains, then hotels."})).json()
        assert edited["skills"][0]["body"] == "Trains, then hotels." and edited["skills"][0]["versions"] == 2
        versions = (await http.get(f"/api/skills/{skill['id']}/versions")).json()["versions"]
        assert [v["body"] for v in versions] == ["Trains, then hotels.", "Trains first."]
        restored = (await http.post(f"/api/skills/{skill['id']}", json={"restore": versions[1]["id"]})).json()
        assert restored["skills"][0]["body"] == "Trains first."

        # Athena's suggestion, working by itself: you change it, then approve it.
        athena.skills.store.suggest(
            skill["id"],
            "Trip plans",
            trip["description"],
            "Trains first.\nNo flights.",
            "You said so",
            "your task “Weekly”",
        )
        listing = (await http.get("/api/skills")).json()
        (suggestion,) = listing["suggestions"]
        assert suggestion["was"] == {"name": "Trip plans", "description": "When I ask for a trip"}
        assert suggestion["diff"] == [{"op": " ", "text": "Trains first."}, {"op": "+", "text": "No flights."}]
        assert (await http.get("/api/overview")).json()["review"] == {"memories": 0, "skills": 1}
        approve = {"action": "approve", "body": "Trains first.\nNo flights, ever."}
        listing = (await http.post(f"/api/skills/suggestions/{suggestion['id']}", json=approve)).json()
        assert listing["suggestions"] == [] and listing["skills"][0]["body"] == "Trains first.\nNo flights, ever."

        new = athena.skills.store.suggest(None, "Recipes", "When I cook", "Metric.", "", "your task “Weekly”")
        listing = (await http.post(f"/api/skills/suggestions/{new}", json={"action": "discard"})).json()
        assert listing["suggestions"] == [] and [s["name"] for s in listing["skills"]] == ["Trip plans"]

        for method, path, body, status in [
            ("POST", "/api/skills", {**trip}, 400),  # one has that name
            ("POST", "/api/skills", {"name": "x", "description": "y"}, 400),
            ("POST", "/api/skills", {**trip, "name": "Long", "body": "x" * 5001}, 400),
            ("POST", f"/api/skills/{skill['id']}", {"restore": "1"}, 400),
            ("POST", f"/api/skills/{skill['id']}", {"restore": 99999}, 400),
            ("POST", "/api/skills/999", {**trip, "name": "Other"}, 400),
            ("GET", "/api/skills/999/versions", None, 404),
            ("GET", "/api/skills/abc", None, 404),
            ("POST", f"/api/skills/suggestions/{new}", {"action": "approve"}, 400),  # gone
            ("POST", "/api/skills/suggestions/1", {"action": "maybe"}, 400),
        ]:
            response = await http.request(method, path, json=body)
            assert response.status_code == status, (method, path, body, response.text)

        listing = (await http.delete(f"/api/skills/{skill['id']}")).json()
        assert listing["skills"] == [] and athena.agent.prompt() == athena.agent.system_prompt
        assert (await http.delete(f"/api/skills/{skill['id']}")).status_code == 400


# -- the board: projects handed to Claude ------------------------------------------------------------------


async def test_the_board_from_the_dashboard(athena, tmp_path):
    from test_claude_projects import REPO, STORIES, Clock, FakeGitHub

    from pi_assistant.claude_projects import BoardStore, ClaudeProjects, ClaudeProjectsConfig

    async with client(athena) as http:
        off = await http.get("/api/projects")
        assert off.status_code == 409 and "switched off" in off.json()["error"]

        github = FakeGitHub()
        github.commit("main", {"CLAUDE.md": "How to work"})
        cfg = ClaudeProjectsConfig(enabled=True, repo=REPO, token="good-token")
        projects = ClaudeProjects(cfg, BoardStore(athena.config.db_path), http=github.client(), clock=Clock(github))
        athena.builtins.running["claude_projects"] = projects
        said = []

        class Telegram:
            chat_id = "111"

            async def tell(self, text):
                said.append(text)

        athena.channel = Telegram()
        project = await projects.hand_over({"title": "Bank statements", "summary": "Sum it up.", "stories": STORIES})
        board = (await http.post("/api/projects/refresh", json={})).json()
        assert board["repo"] == REPO and board["error"] is None and board["poll_minutes"] == 30
        (shown,) = board["projects"]
        assert (shown["title"], shown["status"], shown["number"], [s["id"] for s in shown["stories"]]) == (
            "Bank statements",
            "new",
            1,
            ["S1", "S2"],
        )

        # Restart, from the board.
        board = (await http.post("/api/projects/restart", json={"folder": project.folder})).json()
        assert github.events[-2:] == [("unlabeled", 1, "claude"), ("labeled", 1, "claude")]
        assert (await http.post("/api/projects/restart", json={"folder": "nope"})).status_code == 400

        branch = f"project/{project.folder}"
        github.commit(branch, {f"{project.folder}/status.json": json.dumps({"status": "done", "summary": "Tested."})})
        board = (await http.post("/api/projects/refresh", json={})).json()
        assert board["projects"][0]["status"] == "done"
        assert said == [f"✅ Claude has finished “Bank statements”: Tested. Merge it when you're happy.\n{project.url}"]
        # In your chat too, so you can ask Athena about it.
        assert athena.history.load("111")[-1] == {"role": "assistant", "content": said[0]}
        assert (await http.get("/api/projects")).json()["projects"][0]["status"] == "done"

        # More work, from the board: you wrote it, so it's not asked about again.
        more = {
            "folder": project.folder,
            "summary": "Sharing.",
            "stories": [{"title": "Export", "done_when": ["xlsx"]}],
        }
        board = (await http.post("/api/projects/extend", json=more)).json()
        assert [s["id"] for s in board["projects"][0]["stories"]] == ["S1", "S2", "S3"]
        assert "### S3: Export" in github.files(branch)[f"{project.folder}/Requirements.md"]
        assert github.events[-1] == ("labeled", 1, "claude")
        for bad in [{**more, "folder": "nope"}, {**more, "stories": []}, {**more, "summary": ""}]:
            assert (await http.post("/api/projects/extend", json=bad)).status_code == 400

        # Nothing about you goes to Claude, even what you typed yourself.
        pushed = len(github.events)
        personal = {**more, "notes": "Email it to me at sam@mailbox.co.uk"}
        refused = await http.post("/api/projects/extend", json=personal)
        assert refused.status_code == 400
        assert refused.json()["error"] == (
            "That has an email address (“sam@mailbox.co.uk”) in it. "
            "Take that out: Claude gets only what anyone could ask."
        )
        assert len(github.events) == pushed and "sam@" not in github.files(branch)[f"{project.folder}/Requirements.md"]

        github.token = "revoked"
        board = (await http.post("/api/projects/refresh", json={})).json()
        assert board["error"].startswith("GitHub refused the token") and board["projects"]  # the last board stays
        assert (await http.get("/api/projects/refresh")).status_code == 405


# -- private words ----------------------------------------------------------------------------------


async def test_private_words_from_the_memory_tab(athena):
    async with client(athena) as http:
        shown = (await http.get("/api/memory")).json()["privacy"]
        assert shown == {
            "terms": [],
            "automatic": ["George"],  # [agent] user_name
            "block": ["hand_to_claude", "answer_claude", "extend_claude_project"],
        }
        saved = (await http.post("/api/privacy", json={"terms": ["Elm Tree Road", " acme  widgets", ""]})).json()
        assert saved["terms"] == ["acme widgets", "Elm Tree Road"]
        assert athena.privacy.check("hand_to_claude", {"summary": "near elm tree road"}, asks=True).refused

        too_short = await http.post("/api/privacy", json={"terms": ["Al"]})
        assert too_short.status_code == 400 and "too short" in too_short.json()["error"]
        assert (await http.post("/api/privacy", json={"terms": "Elm"})).status_code == 400
        assert (await http.get("/api/memory")).json()["privacy"]["terms"] == ["acme widgets", "Elm Tree Road"]

        # Trying some text: what's found, but never a secret's value.
        found = (await http.post("/api/privacy/check", json={"text": f"George, ACME Widgets, {PASSWORD}"})).json()
        assert found["findings"] == [
            {"kind": "one of Athena's secrets, from .env", "text": ""},
            {"kind": "your name", "text": "George"},
            {"kind": "one of your private words", "text": "acme widgets"},
        ]
        assert (await http.post("/api/privacy/check", json={"text": "gold prices"})).json() == {"findings": []}
        assert (await http.post("/api/privacy/check", json={})).status_code == 400


def test_every_route_has_a_handler_that_takes_its_parts():
    import inspect
    import re

    from pi_assistant.dashboard import ROUTES, DashboardServer

    for method, path, handler in ROUTES:
        assert method in (None, "GET", "POST", "DELETE"), path
        params = list(inspect.signature(getattr(DashboardServer, handler)).parameters)
        assert params[:3] == ["self", "request", "method"], handler
        assert set(params[3:]) == set(re.compile(path).groupindex), handler


async def test_paths_it_doesnt_know_are_refused(athena):
    async with client(athena) as http:
        assert (await http.get("/api/nothing")).status_code == 404
        assert (await http.post("/api/nothing", json={})).status_code == 405
        assert (await http.get("/api/tools/demo")).status_code == 404  # a server is changed or removed, not read
        assert (await http.post("/api/overview", json={})).status_code == 405
        assert (await http.get("/api/skills/")).status_code == 200  # a trailing slash is fine


# -- watching, on the Tasks tab ---------------------------------------------------------------------


async def test_watching_from_the_tasks_tab(athena):
    async with client(athena) as http:
        tasks = (await http.get("/api/tasks")).json()
        assert tasks["watching"]["items"] == [] and tasks["watching"]["every_minutes"] == 30
        assert tasks["triggers"] == {"on": False, "events": {}}  # no TRIGGERS_TOKEN here

        added = (await http.post("/api/watches", json={"what": "the parcel from Amazon", "days": 3})).json()
        (item,) = added["watching"]["items"]
        assert (item["what"], item["by"]) == ("the parcel from Amazon", "you")
        assert (await http.post("/api/watches", json={"what": "", "days": 3})).status_code == 400
        assert (await http.post("/api/watches", json={"what": "x", "days": "lots"})).status_code == 400

        await http.post("/api/watches/check", json={})  # the scheduler checks as soon as it's free
        await until(lambda: athena.tasks.check_result is not None)
        assert (await http.get("/api/tasks")).json()["watching"]["last"]["outcome"] in ("told you", "nothing new")

        gone = (await http.delete(f"/api/watches/{item['id']}")).json()
        assert gone["watching"]["items"] == []
        assert (await http.delete(f"/api/watches/{item['id']}")).status_code == 404


# -- security updates, on the Tools tab ---------------------------------------------------------------


async def test_a_servers_vulnerable_dependencies_show_on_its_card(athena):
    athena.security.results["demo"] = {
        "checked": 1792500000.0,
        "clean": False,
        "summary": "Found 2 known vulnerabilities in 33 packages",
        "findings": [
            {
                "package": "certifi",
                "version": "2026.7.22",
                "ids": ["GHSA-6q48"],
                "summary": "Bad roots",
                "fixed": "2026.10.19",
            }
        ],
        "updated": [],
        "error": None,
        "fixed": [],
    }
    async with client(athena) as http:
        demo = next(t for t in (await http.get("/api/tools")).json()["tools"] if t["id"] == "demo")
        assert demo["state"] == "attention" and demo["detail"] == "Known vulnerability in certifi"
        assert demo["security"]["findings"][0]["fixed"] == "2026.10.19"
        # The demo server isn't started with uvx, so Athena doesn't lock its dependencies, or fix them.
        refused = await http.post("/api/tools/demo/fix", json={})
        assert refused.status_code == 404 and "started with uvx" in refused.json()["error"]
