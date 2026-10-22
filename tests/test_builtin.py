"""Built-in integrations: each is one class, registered in one place (integrations.py), that
config.toml, the dashboard, the tools Athena runs with, and doctor all take it from."""

import tomllib
from pathlib import Path

import httpx

from athena import app
from athena.builtin import FAIL, OK, Builtin, Section, Unavailable
from athena.catalog import Integration
from athena.config import Config, LLMConfig
from athena.integrations import BUILTINS
from athena.news import NewsReader
from athena.resources import CONFIG_EXAMPLE
from athena.settings import BY_ID, CATALOG
from athena.tools import Tool

REPO = Path(__file__).resolve().parents[1]


def test_each_built_in_is_everywhere_it_should_be():
    example = tomllib.loads(CONFIG_EXAMPLE.read_text())
    for kind in BUILTINS:
        assert issubclass(kind, Builtin)
        # Its section of config.toml, with its settings, there by default and in the example.
        assert Config.model_fields[kind.section].annotation is kind.Settings
        assert isinstance(getattr(Config(llm=LLMConfig(model="m")), kind.section), kind.Settings)
        assert kind.section in example, kind.section
        kind.Settings(**example[kind.section])
        # On the dashboard's Tools tab, with settings it really has.
        assert BY_ID[kind.entry.id] is kind.entry and kind.entry.builtin and kind.entry.section == kind.section
        assert all(f.key.split(".")[0] in kind.Settings.model_fields for f in kind.entry.fields)
    assert [i.id for i in CATALOG if i.builtin] == [k.entry.id for k in BUILTINS]


class WeatherConfig(Section):
    enabled: bool = True
    place: str = "London"


class Weather(Builtin):
    """A built-in that exists only here, to show what registering one gives it."""

    section = "weather"
    Settings = WeatherConfig
    entry = Integration("weather", "Weather", "Today's weather.", "weather", builtin=True)
    closed = 0

    @classmethod
    def settings(cls, cfg):
        return WeatherConfig()  # this test's Config has no weather section: that comes from the registry

    @classmethod
    def start(cls, cfg):
        return cls()

    def tools(self):
        async def weather(args):
            return "Sunny"

        return [Tool("weather_now", "Today's weather", {"type": "object", "properties": {}}, weather)]

    async def close(self):
        Weather.closed += 1

    @classmethod
    async def check(cls, cfg, report):
        report(OK, "it's sunny")


class Broken(Weather):
    entry = Integration("broken", "Broken", "Never starts.", "broken", builtin=True)

    @classmethod
    def start(cls, cfg):
        raise Unavailable("BROKEN_KEY isn't set in .env")


async def test_athena_runs_what_is_registered(config, monkeypatch):
    monkeypatch.setattr(app, "BUILTINS", (*BUILTINS, Weather, Broken))
    config.news.feeds = {"BBC": "https://feeds.example/bbc"}
    builtins = app.Builtins.build(config)
    assert {t.name for t in builtins.tools} >= {"read_news", "weather_now"}
    assert isinstance(builtins.get("news"), NewsReader) and builtins.get("broken") is None
    status = builtins.status()
    assert status["weather"] == (["weather_now"], None)
    assert status["broken"] == ([], "BROKEN_KEY isn't set in .env")
    assert status["trading212"] == ([], None) and status["databases"] == ([], None)  # switched off, or nothing to use
    await builtins.close()
    assert Weather.closed == 1


async def test_doctor_checks_the_news_feeds(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "good.example":
            return httpx.Response(200, content=b"<rss><channel><item><title>One</title></item></channel></rss>")
        return httpx.Response(500)

    config.news.feeds = {"Good": "https://good.example/rss", "Down": "https://down.example/rss"}
    reported = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await NewsReader.check(config, lambda mark, msg: reported.append((mark, msg)), http)
    assert reported[0] == (OK, "Good: 1 item")
    assert reported[1][0] == FAIL and reported[1][1].startswith("Down: HTTPStatusError")
