"""Notifications on your devices: when Athena answers a message sent on the dashboard, tells you something by
itself, such as what a task found, or needs your OK; never while you're looking at the dashboard; and turning
them on and off for a device."""

import asyncio

import httpx
import pytest
from test_chat import answered, chat  # noqa: F401 - chat is a fixture
from test_dashboard import athena, client, until  # noqa: F401 - athena is a fixture
from test_push import Device


@pytest.fixture
def phone(chat):  # noqa: F811
    """A phone that takes Athena's notifications, through a stand-in for Apple's push service."""
    device = Device()
    device.received = []

    def service(request: httpx.Request) -> httpx.Response:
        device.received.append((request.headers["urgency"], device.read(request.content)))
        return httpx.Response(201)

    chat.push.http = httpx.AsyncClient(transport=httpx.MockTransport(service))
    chat.push.subscribe(device.subscription, "https://pi.example")
    return device


async def test_turning_them_on_for_a_device_and_off(chat):  # noqa: F811
    device = Device()
    sent = []
    chat.push.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(201)))
    async with client(chat) as http:
        assert (await http.get("/api/push")).json() == {"key": chat.push.public_key, "devices": 0}
        elsewhere = {**device.subscription, "endpoint": "https://evil.example/x"}
        assert (await http.post("/api/push", json={"subscription": elsewhere})).status_code == 400
        on = await http.post("/api/push", json={"subscription": device.subscription})
        assert on.json() == {"devices": 1}
        endpoint = device.subscription["endpoint"]
        assert (await http.post("/api/push/test", json={"endpoint": endpoint})).json() == {"sent": True}
        assert device.read(sent[0].content)["body"].startswith("Notifications are on.")
        assert (await http.post("/api/push/off", json={"endpoint": endpoint})).json() == {"devices": 0}
        assert (await http.post("/api/push/test", json={"endpoint": endpoint})).status_code == 400


async def test_an_answer_to_a_message_sent_on_the_dashboard(chat, phone):  # noqa: F811
    async with client(chat) as http:
        await http.post("/api/agents/1/messages", json={"text": "Hello"})
        await answered(http)
        await until(lambda: phone.received)
        assert phone.received == [
            ("normal", {"title": "Athena · Master", "body": "Hello there.", "url": "#agents/1", "tag": "reply-1"})
        ]

        # Not while you're looking at the page: it shows the answer itself.
        assert (await http.post("/api/presence", json={"looking": True})).status_code == 200
        await http.post("/api/agents/1/messages", json={"text": "Hello again"})
        await answered(http)
        await asyncio.sleep(0.1)
        assert len(phone.received) == 1
        await http.post("/api/presence", json={"looking": False})  # you've switched away
        await http.post("/api/agents/1/messages", json={"text": "Still there?"})
        await answered(http)
        await until(lambda: len(phone.received) == 2)


async def test_while_the_page_waits_for_news_youre_looking_at_it(chat, phone):  # noqa: F811
    async with client(chat) as http:
        version = (await http.get("/api/status")).json()["version"]
        waiting = asyncio.create_task(http.get(f"/api/status?after={version}&wait=5&looking=1"))
        await until(lambda: chat.dashboard.looking)
        await chat.notify("📋 **Briefing**\n\nAll quiet.")
        await waiting
        assert phone.received == []


async def test_what_athena_tells_you_by_itself_and_when_it_needs_your_ok(chat, phone):  # noqa: F811
    assert chat.channel is None  # no Telegram here: your devices, and the dashboard, are how it reaches you
    assert await chat.notify("📋 **Briefing**\n\nAll quiet.") is True
    assert phone.received == [("normal", {"title": "Athena", "body": "📋 Briefing All quiet.", "url": "#agents",
                                          "tag": "athena"})]  # fmt: skip

    # A task's request for your OK, answered on the dashboard.
    asking = asyncio.create_task(
        chat.reach.ask("send_note", {"text": "hi"}, None, why="For your task “Notes”", timeout=5)
    )
    await until(lambda: len(phone.received) == 2)
    urgency, said = phone.received[1]
    assert urgency == "high" and said["title"] == "Athena needs your OK"
    assert said["body"] == "For your task “Notes”: allow send_note?" and said["url"] == "#agents"
    async with client(chat) as http:
        [request] = (await http.get("/api/agents")).json()["approvals"]
        assert request["why"] == "For your task “Notes”" and request["session"] is None
        await http.post(f"/api/approvals/{request['id']}", json={"allow": True})
    assert await asyncio.wait_for(asking, 2) is True


async def test_only_that_theres_something_new_if_youd_rather(chat, phone):  # noqa: F811
    chat.push.show_text = False
    await chat.notify("Your locker is number 42.")
    assert phone.received[0][1]["body"] == "Something new. Open Athena to see it."
    chat.history.rename(1, "Locker hunting")
    await chat.notify("It's number 42.", session=1)
    assert phone.received[1][1]["title"] == "Athena"  # not where it was said, either


async def test_every_session_notifies_you_unless_you_mute_it(chat, phone):  # noqa: F811
    async with client(chat) as http:
        await http.post("/api/agents/1", json={"name": "Job hunting"})
        muted = await http.post("/api/agents/1", json={"muted": True})
        assert muted.json()["muted"] is True and (await http.get("/api/agents")).json()["sessions"][0]["muted"]
        assert (await http.post("/api/agents/1", json={"muted": "yes"})).status_code == 400
        await http.post("/api/agents/1/messages", json={"text": "Hello"})
        await answered(http)
        assert await chat.notify("📋 **Find jobs**\n\nTwo new ones.", session=1) is False
        await asyncio.sleep(0.1)
        assert phone.received == []  # neither an answer nor what a task found there

        await http.post("/api/agents/1", json={"muted": False})
        assert await chat.notify("📋 **Find jobs**\n\nTwo new ones.", session=1) is True
        assert phone.received[-1][1] == {
            "title": "Athena · Job hunting",
            "body": "📋 Find jobs Two new ones.",
            "url": "#agents/1",
            "tag": "reply-1",
        }

        # What a task asks for your OK for always reaches you, from its session.
        await http.post("/api/agents/1", json={"muted": True})
        asking = asyncio.create_task(
            chat.reach.ask("send_note", {}, None, why="For your task “Find jobs”", timeout=5, session=1)
        )
        await until(lambda: len(phone.received) == 2)
        assert phone.received[1][1]["url"] == "#agents/1"
        [request] = (await http.get("/api/agents")).json()["approvals"]
        assert request["session"] == 1
        await http.post(f"/api/approvals/{request['id']}", json={"allow": False})
        assert await asyncio.wait_for(asking, 2) is False


async def test_a_muted_agent_doesnt_message_you_in_telegram_either(chat, phone):  # noqa: F811
    said = []

    class Telegram:
        async def tell(self, text, *, session=None):
            said.append(text)

    chat.channel = Telegram()
    chat.history.set_muted(1, True)
    assert await chat.notify("📋 **Jobs**\n\nNothing much.", session=1) is False
    assert said == [] and phone.received == []
    assert await chat.notify("Something for you.") is True and said == ["Something for you."]  # not an agent's
    chat.history.set_muted(1, False)
    assert await chat.notify("📋 **Jobs**\n\nTwo new ones.", session=1) is True
    assert said[-1] == "📋 **Jobs**\n\nTwo new ones." and len(phone.received) == 2
