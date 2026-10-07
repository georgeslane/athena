"""The dashboard, served for real on a free local port by Athena's own services."""

import asyncio
import gzip
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
        "reachable": True,
        "context_window": 131072,  # what oMLX is set to, below what the model can do
        "error": None,
        "checked": 0,
    }
    assert overview["tools"] == {"count": 6, "servers": 1, "connected": 1, "applying": False}
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
