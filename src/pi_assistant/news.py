"""Headlines from news feeds you choose: the built-in `read_news` tool.

Only the feeds listed under [news] in config.toml are ever fetched, and all the model
chooses is which of them to read and how many items. So reading the news can't send
anything you've said anywhere, which is why it doesn't ask first, unlike `fetch`.
"""

from __future__ import annotations

import asyncio
import email.utils
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from pi_assistant.config import NewsConfig
from pi_assistant.tools import Tool

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 5_000_000
MAX_ITEMS = 30
SUMMARY_CHARS = 280
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_OLDEST = datetime.min.replace(tzinfo=UTC)


@dataclass
class Item:
    source: str
    title: str
    link: str
    summary: str
    published: datetime | None


class NewsReader:
    def __init__(self, cfg: NewsConfig, timezone: str = "UTC", http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        try:
            self.tz: tzinfo = ZoneInfo(timezone)
        except Exception:  # unknown timezone name
            self.tz = UTC
        self.http = http or httpx.AsyncClient(
            timeout=15, follow_redirects=True, headers={"User-Agent": "pi-assistant news reader"}
        )
        self._cache: dict[str, tuple[float, list[Item]]] = {}

    async def close(self) -> None:
        await self.http.aclose()

    def tools(self) -> list[Tool]:
        sources = list(self.cfg.feeds)

        async def read_news(args: dict[str, Any]) -> str:
            try:
                limit = int(args.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            return await self.read(str(args.get("source") or ""), limit)

        return [
            Tool(
                name="read_news",
                description=(
                    f"Latest news headlines and summaries, newest first, from: {', '.join(sources)}. "
                    "To read a whole article, use fetch on its link."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": sources, "description": "One source (default: all)"},
                        "limit": {
                            "type": "integer",
                            "description": f"How many items (default 10, at most {MAX_ITEMS})",
                        },
                    },
                },
                handler=read_news,
            )
        ]

    async def read(self, source: str = "", limit: int = 10) -> str:
        if source and source not in self.cfg.feeds:
            return f"There's no news source called {source!r}. The sources are: {', '.join(self.cfg.feeds)}."
        names = [source] if source else list(self.cfg.feeds)
        results = await asyncio.gather(*(self._items(name) for name in names), return_exceptions=True)
        items: list[Item] = []
        problems = []
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("News feed %s failed: %s", name, result)
                problems.append(f"({name} couldn't be loaded: {type(result).__name__})")
            else:
                items += result
        items.sort(key=lambda item: item.published or _OLDEST, reverse=True)
        lines = [self._format(item) for item in items[: max(1, min(limit, MAX_ITEMS))]]
        return "\n\n".join(lines + problems) or "No news items found."

    async def _items(self, source: str) -> list[Item]:
        cached = self._cache.get(source)
        if cached and time.monotonic() - cached[0] < self.cfg.cache_minutes * 60:
            return cached[1]
        body = bytearray()
        async with self.http.stream("GET", self.cfg.feeds[source]) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_FEED_BYTES:
                    raise ValueError(f"the feed is bigger than {MAX_FEED_BYTES} bytes")
        items = parse_feed(bytes(body), source)
        self._cache[source] = (time.monotonic(), items)
        return items

    def _format(self, item: Item) -> str:
        when = f"{item.published.astimezone(self.tz):%a %-d %b, %H:%M} · " if item.published else ""
        lines = [f"[{item.source}] {when}{item.title}"]
        if item.summary and item.summary != item.title:
            lines.append(item.summary)
        if item.link:
            lines.append(item.link)
        return "\n".join(lines)


def parse_feed(data: bytes, source: str) -> list[Item]:
    """Items from an RSS 2.0, RSS 1.0 or Atom feed."""
    root = ET.fromstring(data)
    return [_item(el, source) for el in root.iter() if _local(el.tag) in ("item", "entry")]


def _item(el: ET.Element, source: str) -> Item:
    fields: dict[str, ET.Element] = {}
    link = ""
    for child in el:
        name = _local(child.tag)
        fields.setdefault(name, child)
        if name == "link" and not link:
            if child.get("href") and child.get("rel", "alternate") == "alternate":  # Atom
                link = child.get("href", "")
            elif (child.text or "").strip():  # RSS
                link = (child.text or "").strip()
    summary = next((fields[n] for n in ("description", "summary", "content", "encoded") if n in fields), None)
    when = next((fields[n] for n in ("pubDate", "published", "updated", "date") if n in fields), None)
    return Item(
        source=source,
        title=_plain(fields.get("title")) or "(untitled)",
        link=link,
        summary=_shorten(_plain(summary)),
        published=_date(_plain(when)),
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # drop the XML namespace


def _plain(el: ET.Element | None) -> str:
    if el is None:
        return ""
    text = "".join(el.itertext())
    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", text))).strip()


def _shorten(text: str) -> str:
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1].rsplit(" ", 1)[0] + "…"


def _date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)  # RSS
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)  # Atom and RSS 1.0
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
