"""The built-in Trading 212 tools, against a stand-in for Trading 212's API.

The stand-in answers with data shaped the way Trading 212's own description of its API
says (https://docs.trading212.com/_bundle/api.yaml).
"""

import base64
import copy
import json
import re
from typing import Any

import httpx
import pytest
from conftest import FakeLLMServer, completion

from pi_assistant.agent import Agent
from pi_assistant.app import build_services
from pi_assistant.config import Trading212Config
from pi_assistant.history import ConversationStore
from pi_assistant.tools import ToolError, ToolRegistry
from pi_assistant.trading212 import Order, Trading212

APPLE = {"ticker": "AAPL_US_EQ", "name": "Apple Inc", "currency": "USD", "isin": "US0378331005"}
VANGUARD = {"ticker": "VUSAl_EQ", "name": "Vanguard S&P 500", "currency": "GBX", "isin": "IE00B3XXRP09"}
SUMMARY = {
    "id": 12345678,
    "currency": "GBP",
    "totalValue": 6043.21,
    "cash": {"availableToTrade": 795.2, "reservedForOrders": 0, "inPies": 0},
    "investments": {
        "currentValue": 5248.01,
        "totalCost": 4650,
        "unrealizedProfitLoss": 598.01,
        "realizedProfitLoss": 120.5,
    },
}
POSITIONS = [
    {
        "instrument": APPLE,
        "quantity": 12.5,
        "quantityAvailableForTrading": 12.5,
        "quantityInPies": 0,
        "currentPrice": 187.42,
        "averagePricePaid": 151.2345,
        "createdAt": "2025-03-14T09:30:12.123Z",
        "walletImpact": {
            "currency": "GBP",
            "currentValue": 1843.21,
            "totalCost": 1490,
            "unrealizedProfitLoss": 353.21,
            "fxImpact": -12.4,
        },
    },
    {
        "instrument": VANGUARD,
        "quantity": 40,
        "quantityAvailableForTrading": 30,
        "quantityInPies": 10,
        "currentPrice": 8512,
        "averagePricePaid": 7900,
        "createdAt": "2024-01-02T10:00:00Z",
        "walletImpact": {
            "currency": "GBP",
            "currentValue": 3404.8,
            "totalCost": 3160,
            "unrealizedProfitLoss": 244.8,
            "fxImpact": None,
        },
    },
]
PENDING = {
    "id": 42,
    "side": "BUY",
    "type": "LIMIT",
    "status": "NEW",
    "strategy": "QUANTITY",
    "ticker": "VUSAl_EQ",
    "instrument": VANGUARD,
    "quantity": 10,
    "filledQuantity": 0,
    "limitPrice": 8000,
    "timeInForce": "GOOD_TILL_CANCEL",
    "currency": "GBP",
    "createdAt": "2026-10-03T13:02:00Z",
}
INSTRUMENTS = [
    {
        "ticker": "AAPL_US_EQ",
        "name": "Apple Inc",
        "shortName": "AAPL",
        "isin": "US0378331005",
        "currencyCode": "USD",
        "type": "STOCK",
        "workingScheduleId": 1,
        "maxOpenQuantity": 5000,
    },
    {
        "ticker": "APCd_EQ",
        "name": "Apple Inc",
        "shortName": "APC",
        "isin": "US0378331005",
        "currencyCode": "EUR",
        "type": "STOCK",
        "workingScheduleId": 2,
        "maxOpenQuantity": 5000,
    },
    {
        "ticker": "VUSAl_EQ",
        "name": "Vanguard S&P 500",
        "shortName": "VUSA",
        "isin": "IE00B3XXRP09",
        "currencyCode": "GBX",
        "type": "ETF",
        "workingScheduleId": 3,
        "maxOpenQuantity": 10000,
    },
    {
        "ticker": "PNPL_US_EQ",
        "name": "Pineapple Holdings",
        "shortName": "PNPL",
        "isin": "US0000000001",
        "currencyCode": "USD",
        "type": "WARRANT",
        "workingScheduleId": 1,
    },
]
EXCHANGES = [
    {"id": 10, "name": "NASDAQ", "workingSchedules": [{"id": 1, "timeEvents": []}]},
    {"id": 11, "name": "XETRA", "workingSchedules": [{"id": 2, "timeEvents": []}]},
    {"id": 12, "name": "London Stock Exchange", "workingSchedules": [{"id": 3, "timeEvents": []}]},
]
FILLED = {
    "order": {
        **PENDING,
        "id": 7,
        "type": "MARKET",
        "status": "FILLED",
        "ticker": "AAPL_US_EQ",
        "instrument": APPLE,
        "quantity": 2,
        "filledQuantity": 2,
        "limitPrice": None,
        "timeInForce": None,
        "createdAt": "2026-09-30T14:00:00Z",
    },
    "fill": {
        "id": 70,
        "filledAt": "2026-09-30T14:00:01Z",
        "price": 180.5,
        "quantity": 2,
        "type": "TRADE",
        "tradingMethod": "TOTV",
        "walletImpact": {"currency": "GBP", "netValue": 268.4, "realisedProfitLoss": 0, "fxRate": 0.74, "taxes": []},
    },
}
DIVIDEND = {
    "ticker": "AAPL_US_EQ",
    "instrument": APPLE,
    "amount": 2.43,
    "amountInEuro": 2.8,
    "currency": "GBP",
    "quantity": 12.5,
    "grossAmountPerShare": 0.26,
    "tickerCurrency": "USD",
    "paidOn": "2026-08-14T08:00:00Z",
    "type": "ORDINARY",
    "reference": "d1",
}
DEPOSIT = {"type": "DEPOSIT", "amount": 500, "currency": "GBP", "dateTime": "2026-09-01T08:00:00Z", "reference": "t1"}


class FakeTrading212:
    """Trading 212's API, answering from the data above. `reply` changes what a request gets."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.replies: dict[tuple[str, str], list[httpx.Response | Exception]] = {}
        self.data: dict[str, Any] = {
            "/account/summary": SUMMARY,
            "/positions": POSITIONS,
            "/orders": [PENDING],
            "/orders/42": PENDING,
            "/metadata/instruments": INSTRUMENTS,
            "/metadata/exchanges": EXCHANGES,
            "/history/orders": {"items": [FILLED], "nextPagePath": "/api/v0/equity/history/orders?cursor=7&limit=20"},
            "/history/dividends": {"items": [DIVIDEND], "nextPagePath": None},
            "/history/transactions": {"items": [DEPOSIT], "nextPagePath": None},
        }

    def reply(self, method: str, path: str, *answers: httpx.Response | Exception) -> None:
        """Answer with these in turn, repeating the last."""
        self.replies[(method, path)] = list(answers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v0/equity")
        queued = self.replies.get((request.method, path))
        if queued:
            answer = queued.pop(0) if len(queued) > 1 else queued[0]
            if isinstance(answer, Exception):
                raise answer
            return answer
        if request.method == "GET" and path in self.data:
            return httpx.Response(200, json=copy.deepcopy(self.data[path]))
        if request.method == "POST" and path.startswith("/orders/"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "side": "BUY" if body["quantity"] > 0 else "SELL",
                    "type": path.rsplit("/", 1)[1].upper(),
                    "status": "NEW",
                    "ticker": body["ticker"],
                    "instrument": {"AAPL_US_EQ": APPLE, "VUSAl_EQ": VANGUARD}.get(body["ticker"]),
                    "quantity": body["quantity"],
                    "limitPrice": body.get("limitPrice"),
                    "stopPrice": body.get("stopPrice"),
                    "timeInForce": body.get("timeValidity"),
                    "createdAt": "2026-10-04T09:00:00Z",
                },
            )
        if request.method == "DELETE" and path == "/orders/42":
            return httpx.Response(200)
        return httpx.Response(404)

    def sent(self, method: str) -> list[tuple[str, Any]]:
        """The (path, body) of each request with this method."""
        return [
            (r.url.path.removeprefix("/api/v0/equity"), json.loads(r.content) if r.content else None)
            for r in self.requests
            if r.method == method
        ]

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix("/api/v0/equity") for r in self.requests]

    def client(self, **settings: Any) -> Trading212:
        cfg = Trading212Config(**{"enabled": True, "api_key": "KEY", "api_secret": "SECRET", **settings})
        return Trading212(cfg, "Europe/London", http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


@pytest.fixture
def t212() -> FakeTrading212:
    return FakeTrading212()


# -- reading ------------------------------------------------------------------------------------------


async def test_reads_the_whole_portfolio(t212):
    out = await t212.client().portfolio()
    assert out.splitlines() == [
        "Trading 212 live account 12345678, in GBP",
        "Total value: £6,043.21",
        "Cash: £795.20 free to invest (£0.00 reserved for orders, £0.00 in pies)",
        "Investments: worth £5,248.01, cost £4,650.00, +£598.01 (+12.9%) unrealised; +£120.50 realised all-time",
        "",
        "Holdings (2), largest first:",
        "VUSAl_EQ Vanguard S&P 500: 40 at 8,512.00p (avg 7,900.00p) = £3,404.80, +£244.80 (+7.7%), 10 in pies",
        "AAPL_US_EQ Apple Inc: 12.5 at $187.42 (avg $151.23) = £1,843.21, +£353.21 (+23.7%)",
        "",
        "Pending orders (1):",
        "#42 Buy 10 VUSAl_EQ (Vanguard S&P 500), limit 8,000.00p, until cancelled: new, placed 3 Oct 2026 14:02",
    ]
    # Only reading, from the live account, with the key and secret.
    assert {r.method for r in t212.requests} == {"GET"}
    assert {r.url.host for r in t212.requests} == {"live.trading212.com"}
    assert {r.headers["authorization"] for r in t212.requests} == {"Basic " + base64.b64encode(b"KEY:SECRET").decode()}


async def test_a_whole_portfolio_fits_in_a_tool_result(t212):
    """Unlike the JSON the third-party servers return: 60 holdings in under 6,000 characters."""
    t212.data["/positions"] = [
        {**POSITIONS[0], "instrument": {**APPLE, "ticker": f"T{i:02}_US_EQ", "name": f"Company {i}"}} for i in range(60)
    ]
    assert len(await t212.client().portfolio()) < 6000


async def test_older_keys_without_a_secret_go_in_the_header_as_they_are(t212):
    await t212.client(api_secret="").portfolio()
    assert {r.headers["authorization"] for r in t212.requests} == {"KEY"}


async def test_the_practice_account_is_its_own(t212):
    assert (await t212.client(environment="demo").portfolio()).startswith("Trading 212 practice account")
    assert {r.url.host for r in t212.requests} == {"demo.trading212.com"}


async def test_shows_what_it_can_when_a_permission_is_missing(t212):
    t212.reply("GET", "/orders", httpx.Response(403))
    out = await t212.client().portfolio()
    assert "Holdings (2), largest first:" in out
    assert "(Pending orders couldn't be loaded: Trading 212 refused: the API key needs the 'orders:read'" in out


async def test_a_refused_key_is_said_once(t212):
    for path in ("/account/summary", "/positions", "/orders"):
        t212.reply("GET", path, httpx.Response(401))
    with pytest.raises(ToolError, match="didn't accept the API key") as refused:
        await t212.client().portfolio()
    assert "couldn't be loaded" not in str(refused.value)


async def test_waits_once_when_asked_to_slow_down(t212):
    t212.reply(
        "GET", "/account/summary", httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json=SUMMARY)
    )
    assert "Total value: £6,043.21" in await t212.client().portfolio()
    assert t212.paths().count("/account/summary") == 2

    slow = FakeTrading212()
    slow.reply("GET", "/history/dividends", httpx.Response(429, headers={"Retry-After": "30"}))
    with pytest.raises(ToolError, match="try again in 30s"):
        await slow.client().history("dividends")
    assert len(slow.requests) == 1  # it doesn't wait half a minute in the middle of a reply


async def test_reading_again_within_seconds_uses_the_last_answer(t212):
    client = t212.client()
    await client.portfolio()
    await client.portfolio()
    assert len(t212.requests) == 3  # Trading 212 allows the summary only once every 5 seconds


async def test_reads_history(t212):
    client = t212.client()
    assert (await client.history("orders", "AAPL_US_EQ", 500)).splitlines() == [
        "#7 Buy 2 AAPL_US_EQ (Apple Inc), market: filled, placed 30 Sep 2026 15:00;"
        " filled 2 at $180.50 on 30 Sep 2026 15:00, £268.40",
        "(There are older ones: ask for more, or for one ticker.)",
    ]
    assert dict(t212.requests[-1].url.params) == {"limit": "50", "ticker": "AAPL_US_EQ"}
    assert await client.history("dividends") == (
        "14 Aug 2026 09:00 AAPL_US_EQ (Apple Inc): £2.43 for 12.5 shares at $0.2600 each"
    )
    assert await client.history("transactions", "AAPL_US_EQ") == "1 Sep 2026 09:00 deposit: £500.00"
    assert "ticker" not in t212.requests[-1].url.params  # transactions aren't by instrument
    with pytest.raises(ToolError, match="kind must be one of"):
        await client.history("trades")


async def test_finds_instruments_in_a_list_it_keeps(t212):
    client = t212.client()
    assert (await client.find("apple")).splitlines() == [
        "AAPL_US_EQ: Apple Inc (AAPL), stock on NASDAQ, priced in USD, ISIN US0378331005",
        "APCd_EQ: Apple Inc (APC), stock on XETRA, priced in EUR, ISIN US0378331005",
        "PNPL_US_EQ: Pineapple Holdings (PNPL), warrant on NASDAQ, priced in USD, ISIN US0000000001",
    ]
    assert (await client.find("VUSA")).startswith("VUSAl_EQ: Vanguard S&P 500 (VUSA), ETF on London Stock Exchange")
    assert await client.find("zzz") == "Nothing on Trading 212 matches 'zzz'."
    # The list is fetched once a day, and what's searched for never goes to Trading 212.
    assert t212.paths() == ["/metadata/instruments", "/metadata/exchanges"]


# -- orders ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "says", "sends"),
    [
        (
            {
                "side": "buy",
                "ticker": "AAPL_US_EQ",
                "quantity": 2,
                "order_type": "limit",
                "limit_price": 150,
                "time_validity": "good_till_cancelled",
            },
            "Buy 2 Apple Inc (AAPL_US_EQ, NASDAQ, priced in USD) with a limit of $150.00, so at that price or lower,"
            " on your live account. It stays open until it's filled or cancelled. You hold 12.5."
            " That's about $300.00 at that price.",
            (
                "/orders/limit",
                {"ticker": "AAPL_US_EQ", "quantity": 2, "limitPrice": 150, "timeValidity": "GOOD_TILL_CANCEL"},
            ),
        ),
        (
            {"side": "sell", "ticker": "VUSAl_EQ", "quantity": 5},
            "Sell 5 Vanguard S&P 500 (VUSAl_EQ, London Stock Exchange, priced in GBX) at the market price,"
            " on your live account. You hold 40. That's about £425.60 at the last price.",
            ("/orders/market", {"ticker": "VUSAl_EQ", "quantity": -5}),
        ),
        (
            {"side": "sell", "ticker": "AAPL_US_EQ", "quantity": 1.5, "order_type": "stop", "stop_price": 140},
            "Sell 1.5 Apple Inc (AAPL_US_EQ, NASDAQ, priced in USD) at the market price once the price falls to"
            " $140.00, on your live account. It lasts for today only. You hold 12.5."
            " That's about $210.00 at that price.",
            ("/orders/stop", {"ticker": "AAPL_US_EQ", "quantity": -1.5, "stopPrice": 140, "timeValidity": "DAY"}),
        ),
        (
            {
                "side": "buy",
                "ticker": "APCd_EQ",
                "quantity": 3,
                "order_type": "stop_limit",
                "stop_price": 170,
                "limit_price": 172.5,
            },
            "Buy 3 Apple Inc (APCd_EQ, XETRA, priced in EUR) with a limit of €172.50 once the price rises to €170.00,"
            " on your live account. It lasts for today only. You don't hold any yet."
            " That's about €517.50 at that price.",
            (
                "/orders/stop_limit",
                {"ticker": "APCd_EQ", "quantity": 3, "limitPrice": 172.5, "stopPrice": 170, "timeValidity": "DAY"},
            ),
        ),
    ],
)
async def test_orders_are_described_then_sent_as_trading_212_expects(t212, args, says, sends):
    client = t212.client()
    assert await client.describe(Order.from_args(args)) == says
    assert t212.sent("POST") == []  # describing an order sends nothing
    placed = await client.place(Order.from_args(args))
    assert t212.sent("POST") == [sends]
    assert placed.startswith("Trading 212 accepted the order: #99 ")


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"side": "sell", "ticker": "APCd_EQ", "quantity": 1}, "doesn't hold any APCd_EQ"),
        ({"side": "sell", "ticker": "VUSAl_EQ", "quantity": 35}, "holds 40 VUSAl_EQ, of which only 30 can be sold now"),
        ({"side": "buy", "ticker": "NOPE_EQ", "quantity": 1}, "Trading 212 has no instrument 'NOPE_EQ'"),
        ({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 6000}, "at most 5,000 of AAPL_US_EQ"),
        ({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 1, "order_type": "limit"}, "limit_price must be a number"),
        (
            {"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 1, "limit_price": 150},
            "A market order has no limit_price",
        ),
        ({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 0}, "quantity must be more than 0"),
        ({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": "lots"}, "quantity must be a number"),
        ({"side": "hold", "ticker": "AAPL_US_EQ", "quantity": 1}, "side must be buy or sell"),
        ({"side": "buy", "quantity": 1}, "Give the Trading 212 ticker"),
        ({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 1, "order_type": "trailing"}, "order_type must be one of"),
        (
            {
                "side": "buy",
                "ticker": "AAPL_US_EQ",
                "quantity": 1,
                "order_type": "limit",
                "limit_price": 1,
                "time_validity": "week",
            },
            "time_validity must be day or good_till_cancelled",
        ),
    ],
)
async def test_orders_that_cant_work_are_refused_before_anyone_is_asked(t212, args, error):
    with pytest.raises(ToolError, match=re.escape(error)):
        await t212.client().describe(Order.from_args(args))
    assert t212.sent("POST") == []


async def test_without_the_portfolio_permission_trading_212_checks_sales_itself(t212):
    t212.reply("GET", "/positions", httpx.Response(403))
    says = await t212.client().describe(Order.from_args({"side": "sell", "ticker": "APCd_EQ", "quantity": 1}))
    assert says == "Sell 1 Apple Inc (APCd_EQ, XETRA, priced in EUR) at the market price, on your live account."


async def test_without_the_metadata_permission_the_ticker_is_shown_unchecked(t212):
    t212.reply("GET", "/metadata/instruments", httpx.Response(403))
    says = await t212.client().describe(Order.from_args({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 1}))
    assert says.startswith("Buy 1 AAPL_US_EQ (its name couldn't be checked with Trading 212) at the market price")
    assert says.endswith("You hold 12.5. That's about $187.42 at the last price.")


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.ReadTimeout("slow"), "It may or may not have gone through"),
        (httpx.RemoteProtocolError("dropped"), "It may or may not have gone through"),
        (httpx.Response(500), "an error (500). It may or may not have gone through"),
        (httpx.Response(408), "an error (408). It may or may not have gone through"),
        (httpx.ConnectError("no route"), "Couldn't reach Trading 212, so nothing was sent"),
        (httpx.Response(429, headers={"Retry-After": "1"}), "limiting how often it's asked: try again in 1s"),
        (httpx.Response(403), "needs the 'orders:execute' permission"),
        (
            httpx.Response(400, json={"code": "InsufficientFreeForStocksBuy", "clarification": "Not enough cash"}),
            "said no (400): InsufficientFreeForStocksBuy; Not enough cash",
        ),
    ],
)
async def test_an_order_is_sent_once_whatever_happens(t212, answer, error):
    """Trading 212 would place a repeated order twice, so nothing is ever retried."""
    t212.reply("POST", "/orders/market", answer)
    with pytest.raises(ToolError, match=re.escape(error)):
        await t212.client().place(Order.from_args({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 1}))
    assert len(t212.sent("POST")) == 1


async def test_after_an_order_the_portfolio_is_read_afresh(t212):
    client = t212.client()
    await client.portfolio()
    await client.place(Order.from_args({"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 1}))
    await client.portfolio()
    assert t212.paths().count("/positions") == 2


async def test_cancelling_shows_the_order_then_cancels_it(t212):
    client = t212.client()
    assert await client.describe_cancel(42) == (
        "Cancel this order on your live account: #42 Buy 10 VUSAl_EQ (Vanguard S&P 500), limit 8,000.00p,"
        " until cancelled: new, placed 3 Oct 2026 14:02"
    )
    assert await client.cancel(42) == "Trading 212 is cancelling order #42."
    assert t212.sent("DELETE") == [("/orders/42", None)]
    with pytest.raises(ToolError, match="There's no pending order #7"):
        await client.describe_cancel(7)
    t212.reply("DELETE", "/orders/42", httpx.Response(404))
    with pytest.raises(ToolError, match="isn't pending any more"):
        await client.cancel(42)


async def test_the_agent_asks_with_the_description_and_only_then_trades(config, t212):
    tools = ToolRegistry()
    for tool in t212.client().tools():
        tools.add(tool)
    order = {"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 2}
    server = FakeLLMServer(
        [
            completion(None, [("trading212_place_order", order)]),
            completion("Done."),
            completion(None, [("trading212_place_order", order)]),
            completion("OK, I haven't."),
        ]
    )
    history = ConversationStore(config.db_path, config.agent.max_history_messages)
    agent = Agent(config.agent, server.client(config.llm), tools, history)
    asked = []

    async def approve(tool, args, summary=None):
        asked.append((tool, args, summary))
        return True

    async def decline(tool, args, summary=None):
        return False

    try:
        await agent.respond("chat", "buy 2 apple", confirm=approve)
        assert asked == [
            (
                "trading212_place_order",
                order,
                "Buy 2 Apple Inc (AAPL_US_EQ, NASDAQ, priced in USD) at the market price, on your live account."
                " You hold 12.5. That's about $374.84 at the last price.",
            )
        ]
        assert t212.sent("POST") == [("/orders/market", {"ticker": "AAPL_US_EQ", "quantity": 2})]
        assert server.requests[1]["messages"][-1]["content"].startswith("Trading 212 accepted the order: #99")

        await agent.respond("chat", "buy 2 more", confirm=decline)
        assert len(t212.sent("POST")) == 1  # declined, so nothing more was sent
    finally:
        history.close()


def test_tools_exist_only_when_switched_on_with_a_key(config):
    def tools(cfg):
        services = build_services(cfg)
        services.memory.store.close()
        services.history.close()
        return {t.name: t for t in services.tools.all() if t.name.startswith("trading212_")}

    assert tools(config) == {}
    config.trading212.enabled = True
    assert tools(config) == {}  # switched on, but no key in .env yet
    config.trading212.api_key = "KEY"
    found = tools(config)
    assert sorted(found) == [
        "trading212_cancel_order",
        "trading212_find_instrument",
        "trading212_history",
        "trading212_place_order",
        "trading212_portfolio",
    ]
    # Trades always ask first, with a description; reading doesn't.
    asks = {name for name, tool in found.items() if tool.needs_confirmation}
    assert asks == {"trading212_place_order", "trading212_cancel_order"}
    assert all(found[name].preview for name in asks)
