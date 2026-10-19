"""markets.py: prices, history and symbols from Yahoo Finance, with a stand-in for yfinance."""

import asyncio
import logging
import time

import pandas as pd
import pytest

from pi_assistant.builtin import Unavailable
from pi_assistant.config import Config, LLMConfig
from pi_assistant.markets import MAX_ROWS, Markets, MarketsConfig
from pi_assistant.tools import ToolError

APPLE = {
    "shortName": "Apple Inc.",
    "longName": "Apple Inc.",
    "quoteType": "EQUITY",
    "currency": "USD",
    "fullExchangeName": "NasdaqGS",
    "marketState": "PRE",
    "regularMarketPrice": 343.46,
    "regularMarketChange": 1.4599915,
    "regularMarketChangePercent": 0.4268981,
    "regularMarketDayLow": 340.46,
    "regularMarketDayHigh": 347.82,
    "fiftyTwoWeekLow": 243.42,
    "fiftyTwoWeekHigh": 348.15,
    "marketCap": 5012516765696,
    "trailingPE": 39.38761,
    "dividendYield": 0.31,
    "regularMarketTime": 1792180801,  # 16 Oct 2026, 21:00 in London
}
GOLD = {
    "shortName": "Gold Dec 26",
    "quoteType": "FUTURE",
    "currency": "USD",
    "fullExchangeName": "COMEX",
    "marketState": "REGULAR",
    "regularMarketPrice": 4197.6,
    "regularMarketChange": 16.5,
    "regularMarketChangePercent": 0.394633,
    "expireDate": 1798502400,
}
POUND = {
    "shortName": "GBP/USD",
    "quoteType": "CURRENCY",
    "currency": "USD",
    "regularMarketPrice": 1.3315758,
    "regularMarketChange": -0.0014199018,
    "regularMarketChangePercent": -0.106519915,
}


def prices(days, start=100.0, zone="America/New_York"):
    index = pd.date_range("2026-01-01", periods=days, freq="D", tz=zone)
    close = [start + i for i in range(days)]
    return pd.DataFrame(
        {"Open": close, "High": [c + 0.5 for c in close], "Low": [c - 0.5 for c in close], "Close": close},
        index=index,
    )


class FakeYF:
    def __init__(self):
        self.infos = {"AAPL": APPLE, "GC=F": GOLD, "GBPUSD=X": POUND}
        self.histories = {"GC=F": prices(21, 4000.0), "AAPL": prices(60)}
        self.metadata = {"GC=F": {"chartPreviousClose": 3990.0, "currency": "USD"}}
        self.cache = None
        self.asked = []
        self.slow = set()
        self.failing = set()
        yf = self

        class Ticker:
            def __init__(self, symbol):
                self.symbol = symbol
                self.history_metadata = {}

            @property
            def info(self):
                yf.asked.append(("info", self.symbol))
                if self.symbol in yf.slow:
                    time.sleep(2)
                if self.symbol in yf.failing:
                    raise ConnectionError("Yahoo is down")
                return yf.infos.get(self.symbol, {"trailingPegRatio": None})

            def history(self, period, interval, auto_adjust, timeout):
                yf.asked.append(("history", self.symbol, period, interval))
                self.history_metadata = yf.metadata.get(self.symbol, {})
                return yf.histories.get(self.symbol, pd.DataFrame())

        class Search:
            def __init__(self, query, max_results, news_count, lists_count, timeout):
                yf.asked.append(("search", query))
                self.quotes = (
                    [
                        {
                            "symbol": "RR.L",
                            "longname": "Rolls-Royce Holdings plc",
                            "quoteType": "EQUITY",
                            "exchDisp": "London",
                        },
                        {"symbol": "^FTMC", "shortname": "FTSE 250", "quoteType": "INDEX", "exchDisp": "FTSE Index"},
                    ]
                    if "nothing" not in query
                    else []
                )

        self.Ticker, self.Search = Ticker, Search

    def set_tz_cache_location(self, path):
        self.cache = path


@pytest.fixture
def yf():
    return FakeYF()


@pytest.fixture
def markets(yf, tmp_path):
    return Markets(
        MarketsConfig(enabled=True, timeout_seconds=1), yf, cache=tmp_path / "yfinance", timezone="Europe/London"
    )


def tool(markets, name):
    return next(t for t in markets.tools() if t.name == name).handler


async def test_quotes_for_stocks_futures_and_currencies(markets, yf, tmp_path):
    said = await tool(markets, "market_quote")({"symbols": ["aapl", "GC=F", "GBPUSD=X", "NOPE"]})
    assert said.splitlines() == [
        "AAPL: Apple Inc. (stock, NasdaqGS): 343.46 USD; +1.46 (+0.43%) on the previous close; "
        "day's range 340.46–347.82; 52 weeks 243.42–348.15; market cap 5.01T USD; P/E 39.4; "
        "dividend yield 0.31%. (market pre-market, price as of 16 Oct 21:00)",
        "GC=F: Gold Dec 26 (future, COMEX): 4,197.60 USD; +16.50 (+0.39%) on the previous close; "
        "expires 29 Dec 2026. (market open)",
        "GBPUSD=X: GBP/USD (currency): 1.3316 USD; −0.0014 (−0.11%) on the previous close.",
        "NOPE: Yahoo Finance has no price for it. Check the symbol with market_search.",
    ]
    assert yf.cache == str(tmp_path / "yfinance") and (tmp_path / "yfinance").is_dir()  # not ~/.cache


async def test_one_symbol_failing_doesnt_lose_the_rest(markets, yf):
    yf.failing.add("GC=F")
    said = await markets.quote(["AAPL", "GC=F"])
    assert said.splitlines()[1] == "GC=F: couldn't get it (ConnectionError: Yahoo is down)."
    assert said.startswith("AAPL: Apple Inc.")


async def test_only_symbols_can_be_sent(markets, yf):
    quote = tool(markets, "market_quote")
    for bad in [{"symbols": ["AAPL; rm -rf /"]}, {"symbols": ["my address is 12 Elm Road"]}, {"symbols": []}]:
        with pytest.raises(ToolError):
            await quote(bad)
    with pytest.raises(ToolError, match="1 to 10"):
        await quote({"symbols": ["AAPL"] * 11})
    with pytest.raises(ToolError, match="isn't a symbol"):
        await tool(markets, "market_history")({"symbol": "gold price please"})
    with pytest.raises(ToolError, match="60 characters"):
        await tool(markets, "market_search")({"query": "x" * 61})
    assert yf.asked == []  # nothing reached Yahoo
    assert (await quote({"symbols": "AAPL"})).startswith("AAPL:")  # one symbol as a string is fine


async def test_history_from_the_close_before_with_the_high_and_low(markets, yf):
    said = await tool(markets, "market_history")({"symbol": "GC=F", "period": "1mo"})
    lines = said.splitlines()
    assert lines[:4] == [
        "GC=F in USD over 1mo, every 1d (times in America/New_York):",
        "from 3,990.00 (the close before 1 Jan 2026) to 4,020.00 (21 Jan 2026): +30.00 (+0.75%)",
        "high 4,020.50 (21 Jan 2026), low 3,999.50 (1 Jan 2026)",
        "Closes:",
    ]
    assert lines[4] == "1 Jan 2026: 4,000.00" and lines[-1] == "21 Jan 2026: 4,020.00" and len(lines) == 25
    assert ("history", "GC=F", "1mo", "1d") in yf.asked


async def test_long_history_is_thinned_out_evenly_keeping_the_ends(markets, yf):
    said = await markets.history("AAPL", "5y")
    lines = said.splitlines()
    assert lines[1].startswith("from 100.00 (1 Jan 2026) to 159.00")  # no close before it: from the first
    assert lines[3] == f"Closes ({MAX_ROWS} of 60, evenly spaced):"
    closes = lines[4:]
    assert len(closes) == MAX_ROWS and closes[0] == "1 Jan 2026: 100.00" and closes[-1] == "1 Mar 2026: 159.00"
    assert ("history", "AAPL", "5y", "1mo") in yf.asked


async def test_history_with_nothing_and_a_bad_period(markets):
    assert "no prices for it over 1mo" in await markets.history("NOPE", "1mo")
    with pytest.raises(ToolError, match="period must be one of"):
        await markets.history("AAPL", "3w")


async def test_search_finds_symbols(markets):
    assert await tool(markets, "market_search")({"query": "  rolls   royce "}) == (
        "RR.L: Rolls-Royce Holdings plc (stock, London)\n^FTMC: FTSE 250 (index, FTSE Index)"
    )
    assert await markets.search("nothing at all") == "Nothing on Yahoo Finance matches “nothing at all”."


async def test_yahoo_not_answering_is_given_up_on(markets, yf):
    yf.slow.add("AAPL")
    started = time.monotonic()
    said = await markets.quote(["AAPL"])
    assert said == "AAPL: couldn't get it (ToolError: Yahoo Finance didn't answer within 1s.)."
    assert time.monotonic() - started < 1.9


def test_it_starts_only_when_switched_on_and_quiets_yfinance(tmp_path):
    cfg = Config(llm=LLMConfig(model="m"), base_dir=tmp_path)
    assert not Markets.wanted(cfg)
    cfg.markets.enabled = True
    assert Markets.wanted(cfg)
    markets = Markets.start(cfg)  # the real yfinance, which is installed
    assert [t.name for t in markets.tools()] == ["market_quote", "market_history", "market_search"]
    assert not any(t.needs_confirmation for t in markets.tools())
    assert (tmp_path / "data" / "yfinance").is_dir()
    assert logging.getLogger("yfinance").level == logging.CRITICAL


def test_without_yfinance_it_says_why(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "yfinance", None)  # as if it weren't installed
    cfg = Config(llm=LLMConfig(model="m"), base_dir=tmp_path)
    with pytest.raises(Unavailable, match="yfinance isn't installed"):
        Markets.start(cfg)


async def test_several_quotes_are_asked_for_at_once(markets, yf):
    yf.slow.update({"AAPL", "GC=F"})
    markets.cfg.timeout_seconds = 5
    started = time.monotonic()
    await asyncio.wait_for(markets.quote(["AAPL", "GC=F"]), 10)
    assert time.monotonic() - started < 3.5  # two 2-second lookups, side by side
