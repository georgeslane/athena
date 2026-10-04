"""The built-in news reader, with stand-in feeds."""

import json

import httpx
import pytest

from pi_assistant.app import build_services
from pi_assistant.config import NewsConfig
from pi_assistant.news import MAX_FEED_BYTES, NewsReader, parse_feed

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example News</title>
<item><title>Rates held at 4%</title><link>https://news.example/rates</link>
<description><![CDATA[<p>The bank <b>held</b> rates &amp; hinted at cuts.</p>]]></description>
<pubDate>Sun, 04 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>An older story</title><link>https://news.example/old</link>
<pubDate>Fri, 02 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Tech</title>
<entry><title type="html">A new &lt;b&gt;chip&lt;/b&gt;</title>
<link rel="enclosure" href="https://tech.example/chip.mp3"/><link rel="alternate" href="https://tech.example/chip"/>
<summary>It's faster.</summary><updated>2026-10-04T09:30:00Z</updated></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://old.example/1"><title>From RSS 1.0</title><link>https://old.example/1</link>
<dc:date>2026-10-03T07:00:00+01:00</dc:date></item>
</rdf:RDF>"""

FEEDS = {"Example": "https://news.example/rss", "Tech": "https://tech.example/atom", "Old": "https://old.example/rdf"}
BODIES = {FEEDS["Example"]: RSS, FEEDS["Tech"]: ATOM, FEEDS["Old"]: RDF}


def reader_for(feeds: dict[str, str], bodies: dict[str, bytes], requested: list[str] | None = None) -> NewsReader:
    def handler(request: httpx.Request) -> httpx.Response:
        if requested is not None:
            requested.append(str(request.url))
        body = bodies.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NewsReader(NewsConfig(feeds=feeds), "Europe/London", http=http)


def test_reads_rss_atom_and_rss_1():
    rates, older = parse_feed(RSS, "Example")
    assert rates.title == "Rates held at 4%" and rates.link == "https://news.example/rates"
    assert rates.summary == "The bank held rates & hinted at cuts."
    assert rates.published.isoformat() == "2026-10-04T08:00:00+00:00"

    (chip,) = parse_feed(ATOM, "Tech")
    assert (chip.title, chip.link, chip.summary) == ("A new chip", "https://tech.example/chip", "It's faster.")
    assert chip.published.isoformat() == "2026-10-04T09:30:00+00:00"

    (old,) = parse_feed(RDF, "Old")
    assert old.link == "https://old.example/1" and old.published.isoformat() == "2026-10-03T07:00:00+01:00"


async def test_newest_first_across_feeds_in_local_time():
    reader = reader_for(FEEDS, BODIES)
    news = await reader.read(limit=3)
    assert news.split("\n\n") == [
        "[Tech] Sun 4 Oct, 10:30 · A new chip\nIt's faster.\nhttps://tech.example/chip",  # BST
        "[Example] Sun 4 Oct, 09:00 · Rates held at 4%\nThe bank held rates & hinted at cuts.\nhttps://news.example/rates",
        "[Old] Sat 3 Oct, 07:00 · From RSS 1.0\nhttps://old.example/1",
    ]
    await reader.close()


async def test_one_source_and_unknown_sources():
    reader = reader_for(FEEDS, BODIES)
    assert (await reader.read("Example")).count("[Example]") == 2
    assert await reader.read("Daily Nonsense") == (
        "There's no news source called 'Daily Nonsense'. The sources are: Example, Tech, Old."
    )
    await reader.close()


async def test_a_broken_feed_does_not_stop_the_others():
    feeds = {**FEEDS, "Broken": "https://broken.example/rss", "Huge": "https://huge.example/rss"}
    reader = reader_for(feeds, {**BODIES, feeds["Huge"]: b"<rss>" + b" " * MAX_FEED_BYTES})
    news = await reader.read(limit=1)
    assert news.startswith("[Tech]")
    assert "(Broken couldn't be loaded: HTTPStatusError)" in news
    assert "(Huge couldn't be loaded: ValueError)" in news
    await reader.close()


async def test_only_the_configured_feeds_are_ever_fetched_and_they_are_cached():
    requested: list[str] = []
    reader = reader_for(FEEDS, BODIES, requested)
    (tool,) = reader.tools()
    # The model can only name a source: there's no way to give it a URL.
    assert set(tool.parameters["properties"]) == {"source", "limit"}
    assert tool.parameters["properties"]["source"]["enum"] == list(FEEDS)
    assert not tool.needs_confirmation
    await tool.handler({"source": "https://attacker.example/?data=secret"})
    await tool.handler({})
    await tool.handler({"limit": "lots"})
    assert sorted(requested) == sorted(FEEDS.values())  # each fetched once, then cached
    await reader.close()


@pytest.mark.parametrize("feeds", [{}, {"Example": "https://news.example/rss"}])
def test_tool_appears_only_with_feeds(config, feeds):
    config.news = NewsConfig(feeds=feeds)
    services = build_services(config)
    names = [t.name for t in services.tools.all()]
    assert ("read_news" in names) == bool(feeds)
    if feeds:
        schema = json.dumps(services.tools.get("read_news").schema())
        assert "Example" in schema
    services.memory.store.close()
    services.history.close()
