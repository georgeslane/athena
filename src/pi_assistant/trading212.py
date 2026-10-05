"""Your Trading 212 account: the built-in trading212_* tools.

Reading your portfolio, orders and history runs without asking: it only fetches your own
account from Trading 212, and the instrument search runs on a list kept here. Placing or
cancelling an order always asks you first. The approval message says what Trading 212
calls the instrument, so you can see the model picked the right one, and the order is
then sent once and never retried, since Trading 212 would place a repeated order twice.

The API key is the backstop: without its "orders:execute" permission, nothing can trade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from pi_assistant.config import Trading212Config
from pi_assistant.tools import Tool, ToolError

log = logging.getLogger(__name__)

API = "https://{environment}.trading212.com/api/v0/equity"
CACHE_SECONDS = 10  # Trading 212 allows the account summary and pending orders once every 5s
METADATA_HOURS = 24
MAX_WAIT = 10  # when asked to slow down, wait at most this long before trying once more
MAX_HISTORY = 50
MAX_MATCHES = 10
SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}
ORDER_PATHS = {
    "market": "/orders/market",
    "limit": "/orders/limit",
    "stop": "/orders/stop",
    "stop_limit": "/orders/stop_limit",
}
HISTORY = {
    "orders": ("/history/orders", "history:orders"),
    "dividends": ("/history/dividends", "history:dividends"),
    "transactions": ("/history/transactions", "history:transactions"),
}
# What `doctor` reads to see which permissions the key has, besides "account".
PERMISSION_CHECKS = [
    ("/positions", "portfolio", None),
    ("/orders", "orders:read", None),
    *((path, scope, {"limit": 1}) for path, scope in HISTORY.values()),
    ("/metadata/exchanges", "metadata", None),
]


@dataclass
class Instrument:
    ticker: str
    name: str
    short_name: str
    isin: str
    currency: str
    kind: str
    exchange: str
    max_quantity: float | None


@dataclass
class Order:
    """An order as the model asked for it, checked."""

    side: str  # buy or sell
    ticker: str
    quantity: float
    kind: str  # market, limit, stop or stop_limit
    limit_price: float | None
    stop_price: float | None
    until_cancelled: bool

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> Order:
        side = str(args.get("side") or "").lower()
        if side not in ("buy", "sell"):
            raise ToolError("side must be buy or sell.")
        ticker = str(args.get("ticker") or "").strip()
        if not ticker:
            raise ToolError("Give the Trading 212 ticker, such as AAPL_US_EQ: trading212_find_instrument finds it.")
        kind = str(args.get("order_type") or "market").lower().replace("-", "_").replace(" ", "_")
        if kind not in ORDER_PATHS:
            raise ToolError(f"order_type must be one of: {', '.join(ORDER_PATHS)}.")
        limit = _positive(args, "limit_price") if kind in ("limit", "stop_limit") else None
        stop = _positive(args, "stop_price") if kind in ("stop", "stop_limit") else None
        for name, used in (("limit_price", limit), ("stop_price", stop)):
            if used is None and args.get(name) is not None:
                raise ToolError(f"A {kind.replace('_', '-')} order has no {name}. Leave it out, or change order_type.")
        validity = str(args.get("time_validity") or "day").lower()
        if validity not in ("day", "good_till_cancelled"):
            raise ToolError("time_validity must be day or good_till_cancelled.")
        return cls(side, ticker, _positive(args, "quantity"), kind, limit, stop, validity == "good_till_cancelled")

    def body(self) -> dict[str, Any]:
        """The request Trading 212 expects: selling is a negative quantity."""
        body: dict[str, Any] = {
            "ticker": self.ticker,
            "quantity": self.quantity if self.side == "buy" else -self.quantity,
        }
        if self.limit_price is not None:
            body["limitPrice"] = self.limit_price
        if self.stop_price is not None:
            body["stopPrice"] = self.stop_price
        if self.kind != "market":
            body["timeValidity"] = "GOOD_TILL_CANCEL" if self.until_cancelled else "DAY"
        return body


class Trading212:
    def __init__(self, cfg: Trading212Config, timezone: str = "UTC", http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.base = API.format(environment=cfg.environment)
        try:
            self.tz: tzinfo = ZoneInfo(timezone)
        except Exception:  # unknown timezone name
            self.tz = UTC
        self.http = http or httpx.AsyncClient(timeout=cfg.timeout_seconds)
        # Keys made since Trading 212 added secrets use both; older ones go in the header as they are.
        self._auth = httpx.BasicAuth(cfg.api_key, cfg.api_secret) if cfg.api_secret else None
        self._headers = {} if cfg.api_secret else {"Authorization": cfg.api_key}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._instruments: dict[str, Instrument] = {}
        self._instruments_at = 0.0
        self._metadata_lock = asyncio.Lock()

    @property
    def account(self) -> str:
        return "live account" if self.cfg.environment == "live" else "practice account"

    async def close(self) -> None:
        await self.http.aclose()

    def tools(self) -> list[Tool]:
        async def portfolio(args: dict[str, Any]) -> str:
            return await self.portfolio()

        async def history(args: dict[str, Any]) -> str:
            limit = args.get("limit") or 20
            return await self.history(str(args.get("kind") or "orders"), str(args.get("ticker") or ""), limit)

        async def find(args: dict[str, Any]) -> str:
            return await self.find(str(args.get("query") or ""))

        async def place(args: dict[str, Any]) -> str:
            return await self.place(Order.from_args(args))

        async def preview_place(args: dict[str, Any]) -> str:
            return await self.describe(Order.from_args(args))

        async def cancel(args: dict[str, Any]) -> str:
            return await self.cancel(_order_id(args))

        async def preview_cancel(args: dict[str, Any]) -> str:
            return await self.describe_cancel(_order_id(args))

        price = {"type": "number", "description": "In the instrument's currency (pence for GBX)"}
        return [
            Tool(
                name="trading212_portfolio",
                description=(
                    "The user's Trading 212 account: its value and cash, every holding with its profit or loss, "
                    "and pending orders."
                ),
                parameters={"type": "object", "properties": {}},
                handler=portfolio,
            ),
            Tool(
                name="trading212_history",
                description=(
                    "The user's Trading 212 history, newest first: orders and what they filled at, dividends "
                    "received, or deposits and withdrawals (transactions)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(HISTORY), "description": "Default orders"},
                        "ticker": {"type": "string", "description": "Only this instrument (orders and dividends)"},
                        "limit": {"type": "integer", "description": f"How many (default 20, at most {MAX_HISTORY})"},
                    },
                },
                handler=history,
            ),
            Tool(
                name="trading212_find_instrument",
                description=(
                    "Find stocks, ETFs and other instruments on Trading 212 by name, ticker or ISIN, to get the "
                    "exact ticker an order needs."
                ),
                parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                handler=find,
            ),
            Tool(
                name="trading212_place_order",
                description=(
                    "Buy or sell on Trading 212, only when the user asks for that trade. "
                    "They see the order and approve it before it's placed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "side": {"type": "string", "enum": ["buy", "sell"]},
                        "ticker": {"type": "string", "description": "Trading 212 ticker, such as AAPL_US_EQ"},
                        "quantity": {"type": "number", "description": "How many shares; fractions are fine"},
                        "order_type": {
                            "type": "string",
                            "enum": list(ORDER_PATHS),
                            "description": "Default market",
                        },
                        "limit_price": price
                        | {"description": "For limit and stop_limit orders. " + price["description"]},
                        "stop_price": price
                        | {"description": "For stop and stop_limit orders. " + price["description"]},
                        "time_validity": {
                            "type": "string",
                            "enum": ["day", "good_till_cancelled"],
                            "description": "For all but market orders. Default day",
                        },
                    },
                    "required": ["side", "ticker", "quantity"],
                },
                handler=place,
                needs_confirmation=True,
                preview=preview_place,
            ),
            Tool(
                name="trading212_cancel_order",
                description=(
                    "Cancel one of the user's pending Trading 212 orders, by the number trading212_portfolio shows. "
                    "They approve it first."
                ),
                parameters={
                    "type": "object",
                    "properties": {"order_id": {"type": "integer"}},
                    "required": ["order_id"],
                },
                handler=cancel,
                needs_confirmation=True,
                preview=preview_cancel,
            ),
        ]

    async def check(self) -> tuple[dict[str, Any], list[str]]:
        """The account summary, and the reading permissions the key is missing. For `doctor`."""
        summary = await self._get("/account/summary", "account")
        missing = []
        for path, scope, params in PERMISSION_CHECKS:
            try:
                await self._get(path, scope, params)
            except ToolError as exc:
                if f"'{scope}' permission" not in str(exc):
                    raise
                missing.append(scope)
        return summary, missing

    # -- reading ---------------------------------------------------------------------------------

    async def portfolio(self) -> str:
        summary, positions, orders = await asyncio.gather(
            self._get("/account/summary", "account"),
            self._get("/positions", "portfolio"),
            self._get("/orders", "orders:read"),
            return_exceptions=True,
        )
        for result in (summary, positions, orders):
            if isinstance(result, BaseException) and not isinstance(result, ToolError):
                raise result
        if (
            all(isinstance(r, ToolError) for r in (summary, positions, orders))
            and len({str(r) for r in (summary, positions, orders)}) == 1
        ):
            raise summary  # e.g. the key was refused
        lines: list[str] = []
        if isinstance(summary, ToolError):
            lines.append(f"(The account summary couldn't be loaded: {summary})")
        else:
            lines += self._summary_lines(summary)
        if isinstance(positions, ToolError):
            lines.append(f"(Holdings couldn't be loaded: {positions})")
        elif positions:
            held = sorted(positions, key=lambda p: (p.get("walletImpact") or {}).get("currentValue") or 0, reverse=True)
            lines += ["", f"Holdings ({len(held)}), largest first:", *(self._position_line(p) for p in held)]
        else:
            lines += ["", "No holdings."]
        if isinstance(orders, ToolError):
            lines.append(f"(Pending orders couldn't be loaded: {orders})")
        elif orders:
            lines += ["", f"Pending orders ({len(orders)}):", *(self._order_line(o) for o in orders)]
        return "\n".join(lines).strip()

    async def history(self, kind: str, ticker: str = "", limit: Any = 20) -> str:
        if kind not in HISTORY:
            raise ToolError(f"kind must be one of: {', '.join(HISTORY)}.")
        try:
            count = max(1, min(int(limit), MAX_HISTORY))
        except (TypeError, ValueError):
            count = 20
        path, scope = HISTORY[kind]
        params: dict[str, Any] = {"limit": count}
        if ticker and kind != "transactions":
            params["ticker"] = ticker
        page = await self._get(path, scope, params)
        items = page.get("items") or []
        if not items:
            return f"No {kind} found" + (f" for {ticker}." if ticker and kind != "transactions" else ".")
        lines = [getattr(self, f"_{kind}_line")(item) for item in items]
        more = "\n(There are older ones: ask for more, or for one ticker.)" if page.get("nextPagePath") else ""
        return "\n".join(lines) + more

    async def find(self, query: str) -> str:
        wanted = query.strip().lower()
        if not wanted:
            raise ToolError("Say what to look for: a name, ticker or ISIN.")
        matches = []
        for inst in (await self._load_instruments()).values():
            if wanted in (inst.ticker.lower(), inst.short_name.lower(), inst.isin.lower()):
                rank = 0
            elif inst.ticker.lower().startswith(wanted) or inst.name.lower().startswith(wanted):
                rank = 1
            elif wanted in inst.name.lower():
                rank = 2
            else:
                continue
            matches.append((rank, inst.kind not in ("STOCK", "ETF"), len(inst.name), inst.ticker, inst))
        if not matches:
            return f"Nothing on Trading 212 matches {query!r}."
        matches.sort(key=lambda m: m[:4])
        lines = [self._instrument_line(m[-1]) for m in matches[:MAX_MATCHES]]
        if len(matches) > MAX_MATCHES:
            lines.append(f"(and {len(matches) - MAX_MATCHES} more: be more specific)")
        return "\n".join(lines)

    # -- orders: checked and described before you're asked, then sent once --------------------------

    async def describe(self, order: Order) -> str:
        """What the order will do, in words, for the approval message. Refuses orders that can't work."""
        inst = await self._instrument(order.ticker)
        try:
            held, checked = await self._position(order.ticker), True
        except ToolError as exc:  # without the "portfolio" permission, Trading 212 checks it instead
            log.warning("Couldn't check holdings of %s: %s", order.ticker, exc)
            held, checked = None, False
        if order.side == "sell" and checked:
            if held is None:
                raise ToolError(f"The user doesn't hold any {order.ticker}, so there's nothing to sell.")
            available = held.get("quantityAvailableForTrading")
            if available is not None and order.quantity > available:
                raise ToolError(
                    f"The user holds {_qty(held.get('quantity'))} {order.ticker}, of which only "
                    f"{_qty(available)} can be sold now."
                )
        if order.side == "buy" and inst and inst.max_quantity and order.quantity > inst.max_quantity:
            raise ToolError(f"Trading 212 allows at most {_qty(inst.max_quantity)} of {order.ticker} in one order.")

        currency = inst.currency if inst else ((held or {}).get("instrument") or {}).get("currency", "")
        what = f"{order.side.capitalize()} {_qty(order.quantity)} {self._describe_instrument(order.ticker, inst)}"
        how = {
            "market": "at the market price",
            "limit": f"with a limit of {_price(order.limit_price, currency)}, so at that price or "
            + ("lower" if order.side == "buy" else "higher"),
            "stop": f"at the market price once the price {'rises' if order.side == 'buy' else 'falls'} to "
            f"{_price(order.stop_price, currency)}",
            "stop_limit": f"with a limit of {_price(order.limit_price, currency)} once the price "
            f"{'rises' if order.side == 'buy' else 'falls'} to {_price(order.stop_price, currency)}",
        }[order.kind]
        lines = [f"{what} {how}, on your {self.account}."]
        if order.kind != "market":
            lines.append(
                "It stays open until it's filled or cancelled." if order.until_cancelled else "It lasts for today only."
            )
        price = order.limit_price or order.stop_price or (held or {}).get("currentPrice")
        if held:
            lines.append(f"You hold {_qty(held.get('quantity'))}.")
        elif checked:
            lines.append("You don't hold any yet.")
        if price and currency:
            where = "at the last price" if order.kind == "market" else "at that price"
            lines.append(f"That's about {_total(order.quantity * price, currency)} {where}.")
        return " ".join(lines)

    async def place(self, order: Order) -> str:
        placed = await self._change("POST", ORDER_PATHS[order.kind], order.body())
        return f"Trading 212 accepted the order: {self._order_line(placed or {})}"

    async def describe_cancel(self, order_id: int) -> str:
        try:
            order = await self._get(f"/orders/{order_id}", "orders:read", cache=False)
        except ToolError as exc:
            if "couldn't find" in str(exc):
                raise ToolError(f"There's no pending order #{order_id}. trading212_portfolio lists them.") from None
            raise
        return f"Cancel this order on your {self.account}: {self._order_line(order)}"

    async def cancel(self, order_id: int) -> str:
        try:
            await self._change("DELETE", f"/orders/{order_id}")
        except ToolError as exc:
            if "couldn't find" in str(exc):
                raise ToolError(f"Order #{order_id} isn't pending any more: it was filled or cancelled.") from None
            raise
        return f"Trading 212 is cancelling order #{order_id}."

    # -- talking to Trading 212 -------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        scope: str,
        params: dict[str, Any] | None = None,
        cache: bool = True,
        timeout: float | None = None,
    ) -> Any:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        cached = self._cache.get(key)
        if cache and cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        for attempt in (1, 2):
            try:
                response = await self.http.get(
                    self.base + path,
                    params=params,
                    auth=self._auth or httpx.USE_CLIENT_DEFAULT,
                    headers=self._headers,
                    timeout=timeout or httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.TimeoutException:
                raise ToolError("Trading 212 didn't answer in time.") from None
            except httpx.HTTPError as exc:
                raise ToolError(f"Couldn't reach Trading 212 ({type(exc).__name__}).") from None
            wait = _retry_after(response)
            if response.status_code == 429 and attempt == 1 and wait <= MAX_WAIT:
                await asyncio.sleep(wait)
                continue
            break
        data = self._check(response, scope)
        if cache:
            self._cache[key] = (time.monotonic(), data)
        return data

    async def _change(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Places or cancels an order: one request, never repeated."""
        try:
            response = await self.http.request(
                method, self.base + path, json=body, auth=self._auth or httpx.USE_CLIENT_DEFAULT, headers=self._headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ToolError("Couldn't reach Trading 212, so nothing was sent.") from None
        except httpx.HTTPError:
            raise ToolError(_UNSURE) from None
        finally:
            self._cache.clear()  # the holdings and pending orders may have changed
        if response.status_code == 408 or response.status_code >= 500:
            raise ToolError(f"Trading 212 answered with an error ({response.status_code}). " + _UNSURE)
        return self._check(response, "orders:execute")

    def _check(self, response: httpx.Response, scope: str) -> Any:
        status = response.status_code
        if response.is_success:
            return response.json() if response.content else None
        detail = _detail(response)
        if status == 401:
            raise ToolError(
                f"Trading 212 didn't accept the API key. Check it's for the {self.account} and, if it's "
                "limited to certain IP addresses, that this one is allowed."
            )
        if status == 403:
            raise ToolError(
                f"Trading 212 refused: the API key needs the '{scope}' permission and, if it's limited to certain "
                "IP addresses, this one must be among them. Both are under Settings > API in Trading 212."
                + (f" It said: {detail}" if detail else "")
            )
        if status == 404:
            raise ToolError("Trading 212 couldn't find that.")
        if status == 429:
            raise ToolError(
                f"Trading 212 is limiting how often it's asked: try again in {_retry_after(response):.0f}s."
            )
        raise ToolError(f"Trading 212 said no ({status})" + (f": {detail}" if detail else "."))

    # -- instruments ----------------------------------------------------------------------------

    async def _load_instruments(self) -> dict[str, Instrument]:
        async with self._metadata_lock:
            if self._instruments and time.monotonic() - self._instruments_at < METADATA_HOURS * 3600:
                return self._instruments
            found = await self._get("/metadata/instruments", "metadata", cache=False, timeout=60)  # several MB
            try:
                exchanges = await self._get("/metadata/exchanges", "metadata", cache=False)
            except ToolError as exc:
                log.warning("Couldn't load Trading 212's exchanges: %s", exc)
                exchanges = []
            schedules = {s.get("id"): e.get("name") or "" for e in exchanges for s in e.get("workingSchedules") or []}
            self._instruments = {
                i["ticker"]: Instrument(
                    ticker=i["ticker"],
                    name=i.get("name") or i.get("shortName") or i["ticker"],
                    short_name=i.get("shortName") or "",
                    isin=i.get("isin") or "",
                    currency=i.get("currencyCode") or "",
                    kind=i.get("type") or "",
                    exchange=schedules.get(i.get("workingScheduleId"), ""),
                    max_quantity=i.get("maxOpenQuantity"),
                )
                for i in found
                if i.get("ticker")
            }
            self._instruments_at = time.monotonic()
            return self._instruments

    async def _instrument(self, ticker: str) -> Instrument | None:
        """The instrument, or None if its name can't be checked. Refuses tickers Trading 212 doesn't have."""
        try:
            instruments = await self._load_instruments()
        except ToolError as exc:
            log.warning("Couldn't check %s with Trading 212: %s", ticker, exc)
            return None
        if ticker not in instruments:
            raise ToolError(f"Trading 212 has no instrument {ticker!r}. trading212_find_instrument finds the ticker.")
        return instruments[ticker]

    async def _position(self, ticker: str) -> dict[str, Any] | None:
        positions = await self._get("/positions", "portfolio")
        return next((p for p in positions if (p.get("instrument") or {}).get("ticker") == ticker), None)

    # -- formatting ---------------------------------------------------------------------------------

    def _summary_lines(self, s: dict[str, Any]) -> list[str]:
        cur = s.get("currency") or ""
        cash = s.get("cash") or {}
        inv = s.get("investments") or {}
        gain = inv.get("unrealizedProfitLoss")
        reserved, pies = _money(cash.get("reservedForOrders"), cur), _money(cash.get("inPies"), cur)
        return [
            f"Trading 212 {self.account} {s.get('id', '')}, in {cur}",
            f"Total value: {_money(s.get('totalValue'), cur)}",
            f"Cash: {_money(cash.get('availableToTrade'), cur)} free to invest"
            f" ({reserved} reserved for orders, {pies} in pies)",
            f"Investments: worth {_money(inv.get('currentValue'), cur)}, cost {_money(inv.get('totalCost'), cur)},"
            f" {_signed(gain, cur)}{_percent(gain, inv.get('totalCost'))} unrealised;"
            f" {_signed(inv.get('realizedProfitLoss'), cur)} realised all-time",
        ]

    def _position_line(self, p: dict[str, Any]) -> str:
        inst = p.get("instrument") or {}
        wallet = p.get("walletImpact") or {}
        cur, acct = inst.get("currency") or "", wallet.get("currency") or ""
        gain = wallet.get("unrealizedProfitLoss")
        pies = f", {_qty(p.get('quantityInPies'))} in pies" if p.get("quantityInPies") else ""
        change = _signed(gain, acct) + _percent(gain, wallet.get("totalCost"))
        return (
            f"{inst.get('ticker')} {inst.get('name') or ''}: {_qty(p.get('quantity'))}"
            f" at {_price(p.get('currentPrice'), cur)} (avg {_price(p.get('averagePricePaid'), cur)})"
            f" = {_money(wallet.get('currentValue'), acct)}, {change}{pies}"
        )

    def _order_line(self, o: dict[str, Any]) -> str:
        inst = o.get("instrument") or {}
        cur = inst.get("currency") or o.get("currency") or ""
        kind = (o.get("type") or "").lower()
        if o.get("quantity") is not None:
            amount = _qty(abs(o["quantity"]))
        else:
            amount = f"{_money(o.get('value'), o.get('currency'))} of"
        limit, stop = _price(o.get("limitPrice"), cur), _price(o.get("stopPrice"), cur)
        terms = {"limit": f"limit {limit}", "stop": f"stop {stop}", "stop_limit": f"stop {stop}, limit {limit}"}
        lasts = {"GOOD_TILL_CANCEL": ", until cancelled", "DAY": ", today only"}.get(o.get("timeInForce") or "", "")
        name = f" ({inst['name']})" if inst.get("name") else ""
        side, ticker = (o.get("side") or "").capitalize(), o.get("ticker") or inst.get("ticker")
        return (
            f"#{o.get('id')} {side} {amount} {ticker}{name},"
            f" {terms.get(kind, kind)}{lasts if kind in terms else ''}:"
            f" {(o.get('status') or '').lower().replace('_', ' ')}, placed {self._when(o.get('createdAt'))}"
        )

    def _orders_line(self, item: dict[str, Any]) -> str:
        line = self._order_line(item.get("order") or {})
        fill = item.get("fill") or {}
        if fill.get("quantity"):
            cur = ((item.get("order") or {}).get("instrument") or {}).get("currency") or ""
            wallet = fill.get("walletImpact") or {}
            acct = wallet.get("currency") or ""
            realised = wallet.get("realisedProfitLoss")
            line += (
                f"; filled {_qty(abs(fill['quantity']))} at {_price(fill.get('price'), cur)} on"
                f" {self._when(fill.get('filledAt'))}, {_money(wallet.get('netValue'), acct)}"
                + (f", realised {_signed(realised, acct)}" if realised else "")
            )
        return line

    def _dividends_line(self, d: dict[str, Any]) -> str:
        name = (d.get("instrument") or {}).get("name")
        return (
            f"{self._when(d.get('paidOn'))} {d.get('ticker')}{f' ({name})' if name else ''}:"
            f" {_money(d.get('amount'), d.get('currency'))} for {_qty(d.get('quantity'))} shares"
            f" at {_price(d.get('grossAmountPerShare'), d.get('tickerCurrency'))} each"
        )

    def _transactions_line(self, t: dict[str, Any]) -> str:
        kind = (t.get("type") or "").lower().replace("_", " ")
        return f"{self._when(t.get('dateTime'))} {kind}: {_money(t.get('amount'), t.get('currency'))}"

    def _instrument_line(self, inst: Instrument) -> str:
        on = f" on {inst.exchange}" if inst.exchange else ""
        short = f" ({inst.short_name})" if inst.short_name and inst.short_name != inst.ticker else ""
        isin = f", ISIN {inst.isin}" if inst.isin else ""
        kind = "ETF" if inst.kind == "ETF" else inst.kind.lower()
        return f"{inst.ticker}: {inst.name}{short}, {kind}{on}, priced in {inst.currency}{isin}"

    def _describe_instrument(self, ticker: str, inst: Instrument | None) -> str:
        if inst is None:
            return f"{ticker} (its name couldn't be checked with Trading 212)"
        on = f", {inst.exchange}" if inst.exchange else ""
        return f"{inst.name} ({ticker}{on}, priced in {inst.currency})"

    def _when(self, value: Any) -> str:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value or "?")
        moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        return f"{moment.astimezone(self.tz):%-d %b %Y %H:%M}"


_UNSURE = "It may or may not have gone through: check trading212_portfolio and trading212_history before trying again."


def _positive(args: dict[str, Any], name: str) -> float:
    try:
        value = float(args.get(name))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number.") from None
    if not value > 0 or value == float("inf"):
        raise ToolError(f"{name} must be more than 0.")
    return value


def _order_id(args: dict[str, Any]) -> int:
    try:
        return int(args.get("order_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ToolError("order_id must be the order's number, from trading212_portfolio.") from None


def _retry_after(response: httpx.Response) -> float:
    """Seconds until Trading 212 will answer again."""
    value = response.headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return 2.0


def _detail(response: httpx.Response) -> str:
    """Trading 212's explanation of a refusal, if it gave one."""
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:300]
    if isinstance(data, dict):
        parts = [str(data[k]) for k in ("code", "clarification", "message", "error") if data.get(k)]
        return "; ".join(parts)[:300]
    return str(data)[:300]


def _qty(value: Any) -> str:
    if value is None:
        return "?"
    return f"{float(value):,.8f}".rstrip("0").rstrip(".")


def _money(amount: Any, currency: Any, decimals: int = 2) -> str:
    if amount is None:
        return "?"
    if currency == "GBX":
        return f"{amount:,.{decimals}f}p"
    symbol = SYMBOLS.get(currency or "")
    if symbol:
        return f"{'-' if amount < 0 else ''}{symbol}{abs(amount):,.{decimals}f}"
    return f"{amount:,.{decimals}f} {currency or ''}".strip()


def _price(amount: Any, currency: Any) -> str:
    return _money(amount, currency, 2 if amount is None or abs(amount) >= 1 else 4)


def _total(amount: float, currency: str) -> str:
    """A total in pounds rather than pence, for instruments priced in pence."""
    return _money(amount / 100, "GBP") if currency == "GBX" else _money(amount, currency)


def _signed(amount: Any, currency: Any) -> str:
    text = _money(amount, currency)
    return text if amount is None or amount < 0 else f"+{text}"


def _percent(part: Any, whole: Any) -> str:
    return f" ({part / whole * 100:+.1f}%)" if part is not None and whole else ""
