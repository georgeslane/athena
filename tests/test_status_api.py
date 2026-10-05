"""The status API that pi-display-microservice reads, served for real on a free local port."""

import asyncio
import contextlib
import dataclasses
import logging
import time

import httpx
import pytest

from pi_assistant.app import build_services
from pi_assistant.cli import main
from pi_assistant.config import DisplayConfig
from pi_assistant.doctor import FAIL, OK, WARN, check_status_api
from pi_assistant.status import Snapshot, State, StatusTracker
from pi_assistant.status_api import StatusServer, is_loopback


async def serve(tracker=None, **settings):
    cfg = DisplayConfig(**{"port": 0, **settings})
    server = StatusServer(cfg, tracker or StatusTracker(), name="Athena", timezone="Europe/London")
    assert await server.start()
    return server


async def get(server, query="", headers=None, path="/v1/status"):
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.get(f"http://127.0.0.1:{server.port}{path}{query}", headers=headers)


async def test_answers_with_what_the_assistant_is_doing():
    tracker = StatusTracker()
    server = await serve(tracker)
    try:
        tracker.channel = "Telegram"
        tracker.begin("What's on tomorrow?").using("search_memory")
        response = await get(server)
        assert response.status_code == 200 and response.headers["content-type"] == "application/json"
        body = response.json()
        # The contract pi-display-microservice relies on (its README, "Athena's status API").
        assert set(body) == {"api", "version", "now", "assistant", "status"}
        assert body["api"] == 1 and body["version"] == server.version
        assert body["now"] == pytest.approx(time.time(), abs=5)
        assert body["assistant"] == {"name": "Athena", "timezone": "Europe/London"}
        assert set(body["status"]) == {f.name for f in dataclasses.fields(Snapshot)}
        status = body["status"]
        assert (status["state"], status["task"], status["step"], status["tools"], status["channel"]) == (
            "working",
            "What's on tomorrow?",
            "Searching memory",
            ["search_memory"],
            "Telegram",
        )
    finally:
        await server.stop()


async def test_waits_for_a_change_when_asked_and_answers_at_once():
    tracker = StatusTracker()
    server = await serve(tracker)
    try:
        before = (await get(server)).json()["version"]
        asyncio.get_running_loop().call_later(0.2, lambda: tracker.begin("Turn on the heating"))
        started = time.monotonic()
        body = (await get(server, f"?wait=10&after={before}")).json()
        assert 0.15 < time.monotonic() - started < 3  # it answered when things changed, not after 10s
        assert body["status"]["state"] == "working" and body["version"] != before
    finally:
        await server.stop()


async def test_when_nothing_changes_it_answers_after_the_wait():
    server = await serve()
    try:
        version = server.version
        started = time.monotonic()
        body = (await get(server, f"?wait=0.3&after={version}")).json()
        assert 0.25 < time.monotonic() - started < 3
        assert body["version"] == version
    finally:
        await server.stop()


@pytest.mark.parametrize("query", ["?wait=10&after=old-1", "?wait=10", "?after=x"])
async def test_a_board_that_is_behind_hears_at_once(query):
    server = await serve()
    try:
        started = time.monotonic()
        assert (await get(server, query)).status_code == 200
        assert time.monotonic() - started < 2
    finally:
        await server.stop()


async def test_versions_from_before_a_restart_never_match():
    first, second = await serve(), await serve()
    try:
        assert first.version.rsplit("-", 1)[0] != second.version.rsplit("-", 1)[0]
    finally:
        await first.stop()
        await second.stop()


async def test_keeps_your_message_off_the_board_if_you_ask():
    tracker = StatusTracker(show_task=False)
    server = await serve(tracker)
    try:
        tracker.begin("Something private")
        status = (await get(server)).json()["status"]
        assert (status["state"], status["task"]) == ("working", "")
    finally:
        await server.stop()


async def test_a_token_when_there_is_one():
    server = await serve(token="board-token-123")
    try:
        assert (await get(server)).status_code == 401
        assert (await get(server, headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await get(server, headers={"Authorization": "Bearer board-token-123"})).status_code == 200
    finally:
        await server.stop()


async def test_only_listens_beyond_this_machine_with_a_token(caplog):
    server = StatusServer(DisplayConfig(host="0.0.0.0", port=0), StatusTracker(), name="Athena", timezone="UTC")
    assert not await server.start()
    assert "needs [display] token" in caplog.text
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.20") and not is_loopback("athena.local")


async def test_a_port_in_use_is_left_to_the_process_that_has_it(caplog):
    caplog.set_level(logging.INFO)
    bot = await serve()
    try:
        tracker = StatusTracker()
        chat = StatusServer(DisplayConfig(port=bot.port), tracker, name="Athena", timezone="UTC")
        assert not await chat.start()
        assert "is in use" in caplog.text
        assert tracker.on_change is None  # it isn't publishing anything
        tracker.begin("hello").finish()  # and the assistant carries on regardless
        assert (await get(bot)).status_code == 200
        await chat.stop()
    finally:
        await bot.stop()


@pytest.mark.parametrize(
    ("method", "path", "query", "status"),
    [
        ("POST", "/v1/status", "", 405),
        ("GET", "/status", "", 404),
        ("GET", "/v1/status", "?wait=soon", 400),
    ],
)
async def test_other_requests_are_refused(method, path, query, status):
    server = await serve()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.request(method, f"http://127.0.0.1:{server.port}{path}{query}")
        assert response.status_code == status
    finally:
        await server.stop()


async def test_stopping_does_not_wait_for_boards_that_are_waiting():
    server = await serve()
    waiting = asyncio.create_task(get(server, f"?wait=25&after={server.version}"))
    await asyncio.sleep(0.2)
    started = time.monotonic()
    await server.stop()
    with contextlib.suppress(httpx.HTTPError):
        await waiting  # the board sees Athena go away, which is what's happening
    assert time.monotonic() - started < 3


async def test_the_assistant_serves_it_and_doctor_checks_it(config):
    config.display.port = 0
    services = build_services(config)
    try:
        await services.start()
        assert services.status_api and services.status_api.port
        config.display.port = services.status_api.port
        reported = []
        await check_status_api(config, lambda mark, msg: reported.append((mark, msg)))
        assert reported == [(OK, "answering: Athena is idle")]
    finally:
        await services.close()

    reported = []
    await check_status_api(config, lambda mark, msg: reported.append((mark, msg)))
    assert reported[0][0] == WARN and "is the pi-assistant service running?" in reported[0][1]


async def test_doctor_explains_settings_that_cannot_work(config):
    reported = []
    config.display = DisplayConfig(host="0.0.0.0", led=True)
    await check_status_api(config, lambda mark, msg: reported.append((mark, msg)))
    assert reported == [
        (WARN, "[display] led is no longer used here: set led in pi-display-microservice's config.toml instead"),
        (FAIL, "it won't listen on 0.0.0.0 without [display] token"),
    ]


def test_snapshots_start_idle():
    assert Snapshot().state is State.IDLE


@pytest.mark.parametrize("args", [[], ["--demo"], ["--preview", "board.png", "--once"]])
def test_the_old_display_command_says_where_the_board_went(args, capsys):
    with pytest.raises(SystemExit) as exited:
        main(["display", *args])
    assert exited.value.code == 1
    assert "pi-display-microservice" in capsys.readouterr().err
