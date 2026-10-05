import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pi_assistant.config import MCPServerConfig
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.tools import ToolRegistry

DEMO = str(Path(__file__).parent / "fixtures" / "demo_mcp_server.py")


def demo_stdio(**kwargs) -> MCPServerConfig:
    return MCPServerConfig(command=sys.executable, args=[DEMO], **kwargs)


async def test_stdio_server_tools_filters_and_calls():
    manager = MCPManager(
        {"demo": demo_stdio(exclude=["hidden_*"], confirm=["delete_*"])},
    )
    await manager.start()
    try:
        tools = {t.name: t for t in manager.tools()}
        assert set(tools) == {"add", "delete_note", "explode", "get_env"}
        assert tools["delete_note"].needs_confirmation
        assert not tools["add"].needs_confirmation
        assert tools["add"].source == "mcp:demo"
        assert tools["add"].parameters["properties"]["a"]["type"] == "integer"

        assert await tools["add"].handler({"a": 2, "b": 40}) == "42"
        assert (await tools["explode"].handler({})).startswith("Error:")

        [status] = manager.status()
        assert status.connected and status.tools == 4

        # Reload reconnects (new subprocess) and tools keep working.
        await manager.reload()
        assert await manager.tools()[0].handler({"a": 1, "b": 1}) == "2"
    finally:
        await manager.stop()
    assert manager.tools() == []


async def test_include_filter_and_name_collisions():
    manager = MCPManager({"one": demo_stdio(include=["add"]), "two": demo_stdio(include=["add"])})
    registry = ToolRegistry()
    registry.add_provider(manager.tools)
    await manager.start()
    try:
        assert [t.name for t in registry.all()] == ["add", "two__add"]
        assert all(t.needs_confirmation for t in registry.all())  # no `confirm` set: every tool asks first
        assert await registry.get("two__add").handler({"a": 3, "b": 4}) == "7"
    finally:
        await manager.stop()


async def test_local_servers_dont_see_our_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    manager = MCPManager({"demo": demo_stdio(include=["get_env"], env={"WANTED": "yes"})})
    await manager.start()
    try:
        [get_env] = manager.tools()
        assert await get_env.handler({"name": "TELEGRAM_BOT_TOKEN"}) == "(unset)"
        assert await get_env.handler({"name": "LLM_API_KEY"}) == "(unset)"
        assert await get_env.handler({"name": "WANTED"}) == "yes"  # `env` from the config is passed on
    finally:
        await manager.stop()


async def test_broken_servers_dont_stop_the_others():
    manager = MCPManager(
        {
            "missing": MCPServerConfig(command="/nonexistent/binary", timeout_seconds=10),
            "unreachable": MCPServerConfig(url="http://127.0.0.1:9/mcp", timeout_seconds=5),
            "demo": demo_stdio(include=["add"]),
            "off": demo_stdio(enabled=False),
        }
    )
    await manager.start()
    try:
        status = {s.name: s for s in manager.status()}
        assert set(status) == {"missing", "unreachable", "demo"}
        assert not status["missing"].connected and status["missing"].error
        assert not status["unreachable"].connected and status["unreachable"].error
        assert status["demo"].connected
        assert [t.name for t in manager.tools()] == ["add"]
    finally:
        await manager.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, DEMO, "http", str(port)], stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(10)


async def test_streamable_http_server(http_server):
    manager = MCPManager({"remote": MCPServerConfig(url=http_server, headers={"Authorization": "Bearer t"})})
    await manager.start()
    try:
        [status] = manager.status()
        assert status.connected, status.error
        add = next(t for t in manager.tools() if t.name == "add")
        assert await add.handler({"a": 5, "b": 6}) == "11"
    finally:
        await asyncio.wait_for(manager.stop(), 20)


async def test_changing_one_server_leaves_the_others_connected():
    manager = MCPManager({"one": demo_stdio(include=["add"]), "two": demo_stdio(include=["get_env"])})
    await manager.start()
    try:
        one, two = (manager._connections[name].client for name in ("one", "two"))
        [status_two] = [s for s in manager.status() if s.name == "two"]
        assert [name for name, _ in status_two.offered] == ["add", "delete_note", "hidden_tool", "explode", "get_env"]

        # Change "two", add "three", leave "one" alone.
        await manager.configure(
            {
                "one": demo_stdio(include=["add"]),
                "two": demo_stdio(include=["add"]),
                "three": demo_stdio(include=["delete_note"], confirm=[]),
            }
        )
        assert manager._connections["one"].client is one  # the same connection
        assert manager._connections["two"].client is not two
        assert [t.name for t in manager.tools()] == ["add", "two__add", "delete_note"]  # in config order
        assert not manager.tools()[2].needs_confirmation

        # Remove "one": its tool goes, and "two"'s add is now plain "add".
        await manager.configure({"two": demo_stdio(include=["add"]), "three": demo_stdio(include=["delete_note"])})
        assert [t.name for t in manager.tools()] == ["add", "delete_note"]
        assert await manager.tools()[0].handler({"a": 1, "b": 2}) == "3"

        # Switched off is the same as removed.
        await manager.configure({"two": demo_stdio(include=["add"], enabled=False)})
        assert manager.tools() == [] and manager.status() == []

        await manager.configure({"two": demo_stdio(include=["add"])}, reconnect=True)
        assert [t.name for t in manager.tools()] == ["add"]
    finally:
        await manager.stop()


async def test_stopping_doesnt_wait_for_a_server_that_never_answers(tmp_path):
    silent = tmp_path / "silent.py"
    silent.write_text("import time\ntime.sleep(60)\n")  # starts, but never speaks MCP
    manager = MCPManager({"silent": MCPServerConfig(command=sys.executable, args=[str(silent)], timeout_seconds=60)})
    starting = asyncio.create_task(manager.start())
    await asyncio.sleep(0.5)
    began = time.monotonic()
    await asyncio.wait_for(manager.stop(), 10)
    await asyncio.wait_for(starting, 5)
    assert time.monotonic() - began < 10 and manager.tools() == []
