"""Market data from Yahoo Finance, through yfinance: the built-in `market_quote`, `market_history` and
`market_search` tools, for stocks, funds, indices, currencies and commodities.

What goes to Yahoo is only ticker symbols, which must look like one (letters, digits and ^ = . -), and
short searches for one. So, like the news, it doesn't ask first. yfinance is imported only when this
is switched on, as it brings pandas with it, and it keeps its cache in Athena's data folder, as
everywhere else is read-only to Athena under systemd.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from zoneinfo import ZoneInfo

from athena.builtin import FAIL, OK, Builtin, Report, Section, Unavailable
from athena.catalog import Integration
from athena.tools import Tool, ToolError

if TYPE_CHECKING:
    from athena.config import Config

log = logging.getLogger(__name__)

MAX_SYMBOLS = 10
MAX_ROWS = 40  # of history: more is thinned out evenly, keeping the first and last
MAX_QUERY_CHARS = 60
SEARCH_RESULTS = 8
_SYMBOL = re.compile(r"^[A-Z0-9^=.\-]{1,24}$")
# For each period, the spacing of prices that gives a useful number of them.
PERIODS = {
    "1d": "15m",
    "5d": "1h",
    "1mo": "1d",
    "3mo": "1d",
    "6mo": "1d",
    "ytd": "1d",
    "1y": "1wk",
    "2y": "1wk",
    "5y": "1mo",
    "10y": "1mo",
    "max": "3mo",
}
KINDS = {
    "EQUITY": "stock",
    "ETF": "ETF",
    "MUTUALFUND": "fund",
    "INDEX": "index",
    "FUTURE": "future",
    "CURRENCY": "currency",
    "CRYPTOCURRENCY": "crypto",
}
MARKET_STATES = {"PRE": "pre-market", "REGULAR": "open", "POST": "after hours", "POSTPOST": "closed"}
SYMBOLS_HELP = (
    "Symbols are Yahoo Finance's: stocks like AAPL or VOD.L (.L for London), indices start with ^ (^GSPC is "
    "the S&P 500, ^FTSE the FTSE 100), commodity futures end in =F (GC=F gold, CL=F crude oil, SI=F silver), "
    "currencies end in =X (GBPUSD=X) and crypto is like BTC-USD. If unsure, use market_search first."
)


class MarketsConfig(Section):
    # Prices and history for stocks, funds, indices, currencies and commodities, from Yahoo Finance
    # (see docs/tools.md). Only ticker symbols and short searches are sent, so it doesn't ask first.
    enabled: bool = False
    timeout_seconds: float = 20.0


class Markets(Builtin):
    section = "markets"
    Settings = MarketsConfig
    entry = Integration(
        "markets",
        "Markets",
        "Prices and history for stocks, funds, indices, currencies and commodities, from Yahoo Finance.",
        "markets",
        builtin=True,
        asks="Never asks: it sends Yahoo only ticker symbols, and short searches for them.",
    )

    @classmethod
    def start(cls, cfg: Config) -> Self:
        try:
            import yfinance
        except ImportError as exc:
            raise Unavailable(f"yfinance isn't installed ({exc}): run athena update") from None
        return cls(cfg.markets, yfinance, cache=cfg.resolve(cfg.data_dir) / "yfinance", timezone=cfg.agent.timezone)

    @classmethod
    async def check(cls, cfg: Config, report: Report) -> None:
        try:
            markets = cls.start(cfg)
            report(OK, (await markets.quote(["^GSPC"])).splitlines()[0])
        except Exception as exc:
            report(FAIL, f"Yahoo Finance: {type(exc).__name__}: {exc}")

    def __init__(self, cfg: MarketsConfig, yf: Any, *, cache: Path | None = None, timezone: str = "UTC"):
        self.cfg = cfg
        self.yf = yf  # the yfinance module, or a stand-in for it in the tests
        if cache is not None:
            cache.mkdir(parents=True, exist_ok=True)
            yf.set_tz_cache_location(str(cache))  # it would use ~/.cache, which Athena can't write to
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # what fails is told to the model instead
        try:
            self.tz: tzinfo = ZoneInfo(timezone)
        except Exception:
            self.tz = UTC

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """yfinance blocks, so it runs in a thread, given up on after timeout_seconds."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), self.cfg.timeout_seconds)
        except TimeoutError:
            raise ToolError(f"Yahoo Finance didn't answer within {self.cfg.timeout_seconds:.0f}s.") from None

    # -- quotes ---------------------------------------------------------------------------------

    async def quote(self, symbols: list[str]) -> str:
        symbols = [_symbol(s) for s in symbols]
        if not symbols or len(symbols) > MAX_SYMBOLS:
            raise ToolError(f"Give 1 to {MAX_SYMBOLS} symbols.")
        found = await asyncio.gather(
            *(self._run(lambda s=s: self.yf.Ticker(s).info) for s in symbols), return_exceptions=True
        )
        return "\n".join(self._describe(s, info) for s, info in zip(symbols, found, strict=True))

    def _describe(self, symbol: str, info: Any) -> str:
        if isinstance(info, BaseException):
            return f"{symbol}: couldn't get it ({type(info).__name__}: {info})."
        price = (info or {}).get("regularMarketPrice")
        if price is None:
            return f"{symbol}: Yahoo Finance has no price for it. Check the symbol with market_search."
        name = info.get("longName") or info.get("shortName") or symbol
        kind = KINDS.get(str(info.get("quoteType")), str(info.get("quoteType") or "").lower())
        where = ", ".join(p for p in (kind, info.get("fullExchangeName") or info.get("exchange")) if p)
        currency = info.get("currency") or ""
        parts = [f"{symbol}: {name} ({where}): {_num(price)} {currency}".rstrip()]
        change, percent = info.get("regularMarketChange"), info.get("regularMarketChangePercent")
        if change is not None and percent is not None:
            parts.append(f"{_signed(change, price)} ({_signed(percent, percent=True)}%) on the previous close")
        low, high = info.get("regularMarketDayLow"), info.get("regularMarketDayHigh")
        if low is not None and high is not None:
            parts.append(f"day's range {_num(low)}–{_num(high)}")
        low, high = info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
        if low is not None and high is not None:
            parts.append(f"52 weeks {_num(low)}–{_num(high)}")
        if info.get("marketCap"):
            parts.append(f"market cap {_big(info['marketCap'])} {currency}".rstrip())
        if info.get("trailingPE"):
            parts.append(f"P/E {info['trailingPE']:.1f}")
        if info.get("dividendYield"):
            parts.append(f"dividend yield {info['dividendYield']:.2f}%")
        if info.get("expireDate"):
            parts.append(f"expires {datetime.fromtimestamp(info['expireDate'], UTC):%-d %b %Y}")
        state = MARKET_STATES.get(str(info.get("marketState")), str(info.get("marketState") or "").lower())
        when = info.get("regularMarketTime")
        tail = []
        if state:
            tail.append(f"market {state}")
        if when:
            tail.append(f"price as of {datetime.fromtimestamp(when, self.tz):%-d %b %H:%M}")
        return "; ".join(parts) + (f". ({', '.join(tail)})" if tail else ".")

    # -- history --------------------------------------------------------------------------------

    async def history(self, symbol: str, period: str) -> str:
        symbol = _symbol(symbol)
        if period not in PERIODS:
            raise ToolError(f"period must be one of {', '.join(PERIODS)}.")
        interval = PERIODS[period]
        ticker = self.yf.Ticker(symbol)
        frame = await self._run(
            lambda: ticker.history(
                period=period, interval=interval, auto_adjust=False, timeout=self.cfg.timeout_seconds
            )
        )
        if frame is None or frame.empty or "Close" not in frame:
            return f"{symbol}: Yahoo Finance has no prices for it over {period}. Check the symbol with market_search."
        frame = frame.dropna(subset=["Close"])
        if frame.empty:
            return f"{symbol}: Yahoo Finance has no prices for it over {period}."
        intraday = interval.endswith(("m", "h"))

        def when(stamp: Any) -> str:
            return stamp.strftime("%-d %b %H:%M" if intraday else "%-d %b %Y")

        meta = getattr(ticker, "history_metadata", None) or {}
        end = frame["Close"].iloc[-1]
        # The change over the period is from the close before it began, as Yahoo gives it, else the first.
        start = meta.get("chartPreviousClose")
        since = f"the close before {when(frame.index[0])}"
        if not start:
            start, since = frame["Close"].iloc[0], when(frame.index[0])
        high_at, low_at = frame["High"].idxmax(), frame["Low"].idxmin()
        zone = getattr(frame.index, "tz", None)
        currency = f" in {meta['currency']}" if meta.get("currency") else ""
        lines = [
            f"{symbol}{currency} over {period}, every {interval} (times in {zone or 'the exchange’s time'}):",
            f"from {_num(start)} ({since}) to {_num(end)} ({when(frame.index[-1])}): "
            f"{_signed(end - start, start)} ({_signed((end - start) / start * 100, percent=True)}%)",
            f"high {_num(frame['High'].max())} ({when(high_at)}), low {_num(frame['Low'].min())} ({when(low_at)})",
            "Closes:",
        ]
        rows = list(range(len(frame)))
        if len(rows) > MAX_ROWS:
            step = (len(rows) - 1) / (MAX_ROWS - 1)
            rows = sorted({round(i * step) for i in range(MAX_ROWS)})
            lines[-1] = f"Closes ({len(rows)} of {len(frame)}, evenly spaced):"
        lines += [f"{when(frame.index[i])}: {_num(frame['Close'].iloc[i])}" for i in rows]
        return "\n".join(lines)

    # -- search ---------------------------------------------------------------------------------

    async def search(self, query: str) -> str:
        query = " ".join(query.split())
        if not query or len(query) > MAX_QUERY_CHARS:
            raise ToolError(f"Search for a name or symbol, in up to {MAX_QUERY_CHARS} characters.")
        found = await self._run(
            lambda: (
                self.yf.Search(
                    query, max_results=SEARCH_RESULTS, news_count=0, lists_count=0, timeout=self.cfg.timeout_seconds
                ).quotes
            )
        )
        if not found:
            return f"Nothing on Yahoo Finance matches “{query}”."
        lines = []
        for q in found[:SEARCH_RESULTS]:
            kind = KINDS.get(str(q.get("quoteType")), str(q.get("quoteType") or "").lower())
            name = q.get("longname") or q.get("shortname") or ""
            where = ", ".join(p for p in (kind, q.get("exchDisp")) if p)
            lines.append(f"{q.get('symbol')}: {name} ({where})")
        return "\n".join(lines)

    # -- tools ----------------------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        async def market_quote(args: dict[str, Any]) -> str:
            symbols = args.get("symbols")
            if isinstance(symbols, str):
                symbols = [symbols]
            if not isinstance(symbols, list):
                raise ToolError('Give the symbols as a list, like ["AAPL", "^FTSE"].')
            return await self.quote([str(s) for s in symbols])

        async def market_history(args: dict[str, Any]) -> str:
            return await self.history(str(args.get("symbol") or ""), str(args.get("period") or "1mo"))

        async def market_search(args: dict[str, Any]) -> str:
            return await self.search(str(args.get("query") or ""))

        return [
            Tool(
                name="market_quote",
                description=(
                    "The latest price of stocks, funds, indices, currencies or commodities, from Yahoo Finance: "
                    f"the change on the day, the day's and year's range, and for stocks the market cap, P/E and "
                    f"dividend yield. {SYMBOLS_HELP}"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": f"Up to {MAX_SYMBOLS} symbols",
                        }
                    },
                    "required": ["symbols"],
                },
                handler=market_quote,
            ),
            Tool(
                name="market_history",
                description=(
                    "How one symbol's price has moved over a period, from Yahoo Finance: the change, the high and "
                    "low, and the closing prices along the way. Symbols as for market_quote."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "period": {"type": "string", "enum": list(PERIODS), "description": "Default 1mo"},
                    },
                    "required": ["symbol"],
                },
                handler=market_history,
            ),
            Tool(
                name="market_search",
                description="Find Yahoo Finance symbols by name, such as “Rolls-Royce”, “gold” or “FTSE 250”.",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "A name or symbol"}},
                    "required": ["query"],
                },
                handler=market_search,
            ),
        ]


def _symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if not _SYMBOL.match(symbol):
        raise ToolError(f"“{raw[:40]}” isn't a symbol. {SYMBOLS_HELP}")
    return symbol


def _num(value: float, like: float | None = None) -> str:
    """A price, with as many decimals as it needs (or as ``like``, the price a change is of): 4,197.60, 1.3316,
    0.004512."""
    value, scale = float(value), abs(float(value if like is None else like))
    if scale >= 100:
        return f"{value:,.2f}"
    if scale >= 1:
        text = f"{value:,.4f}"
        return (
            text.rstrip("0").rstrip(".") if "." in text and len(text.rstrip("0").split(".")[1]) > 2 else f"{value:,.2f}"
        )
    return f"{value:.6g}"


def _signed(value: float, like: float | None = None, percent: bool = False) -> str:
    """A change, with its sign: +16.50, −0.00142, or as a percentage, +0.39."""
    text = f"{abs(float(value)):,.2f}" if percent else _num(abs(float(value)), like)
    return f"+{text}" if value >= 0 else f"−{text}"


def _big(value: float) -> str:
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= size:
            return f"{value / size:,.2f}{suffix}"
    return f"{value:,.0f}"
