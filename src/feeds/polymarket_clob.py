from __future__ import annotations

"""
Polymarket CLOB websocket feed.

Subscribes to orderbook snapshots and trade fills for a set of market token
IDs. Maintains a local order book and publishes CLOBTick and FillEvent to
registered callbacks and the database.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import websockets

from src.net.ssl import create_ssl_context

log = logging.getLogger(__name__)


@dataclass
class CLOBLevel:
    price: float
    size: float


@dataclass
class CLOBTick:
    market_id: str       # condition_id from Gamma
    token_id: str        # outcome token (YES/UP token)
    ts_ms: int
    received_ms: int
    bids: list[CLOBLevel]
    asks: list[CLOBLevel]
    best_bid: float
    best_ask: float
    mid: float


@dataclass
class FillEvent:
    market_id: str
    token_id: str
    ts_ms: int
    received_ms: int
    price: float
    size: float
    side: str            # "BUY" or "SELL"
    taker_order_id: str
    maker_order_id: str


class PolymarketCLOBFeed:
    """
    Subscribes to Polymarket CLOB websocket for a list of market/token pairs.

    Polymarket CLOB websocket protocol:
    - Connect to wss://ws-subscriptions-clob.polymarket.com/ws/market
    - Send subscription message: {"type": "subscribe", "channel": "book", "assets_ids": [...]}
    - Receive: book snapshots (type=book) and price level updates (type=price_change)
    - Also subscribe to "last_trade_price" channel for fills

    Each market has two tokens (YES/UP and NO/DOWN). Subscribe to both when
    execution logic needs executable prices for both outcomes.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._token_ids: set[str] = set()
        self._market_for_token: dict[str, str] = {}
        self._books: dict[str, dict] = {}  # token_id -> {bids: [...], asks: [...]}
        self._tick_callbacks: list[Callable[[CLOBTick], Awaitable[None]]] = []
        self._fill_callbacks: list[Callable[[FillEvent], Awaitable[None]]] = []
        self._ws = None
        self._running = False

    def subscribe_market(self, market_id: str, up_token_id: str, down_token_id: str | None = None) -> None:
        token_ids = [up_token_id]
        if down_token_id:
            token_ids.append(down_token_id)

        for token_id in token_ids:
            self._token_ids.add(token_id)
            self._market_for_token[token_id] = market_id
            self._books[token_id] = {"bids": {}, "asks": {}}

    def unsubscribe_market(self, up_token_id: str, down_token_id: str | None = None) -> None:
        token_ids = [up_token_id]
        if down_token_id:
            token_ids.append(down_token_id)

        for token_id in token_ids:
            self._token_ids.discard(token_id)
            self._market_for_token.pop(token_id, None)
            self._books.pop(token_id, None)

    def add_tick_callback(self, fn: Callable[[CLOBTick], Awaitable[None]]) -> None:
        self._tick_callbacks.append(fn)

    def add_fill_callback(self, fn: Callable[[FillEvent], Awaitable[None]]) -> None:
        self._fill_callbacks.append(fn)

    async def _dispatch_tick(self, tick: CLOBTick) -> None:
        for cb in self._tick_callbacks:
            try:
                await cb(tick)
            except Exception:
                log.exception("CLOB tick callback error")

    async def _dispatch_fill(self, fill: FillEvent) -> None:
        for cb in self._fill_callbacks:
            try:
                await cb(fill)
            except Exception:
                log.exception("CLOB fill callback error")

    def _build_tick(self, token_id: str, ts_ms: int) -> CLOBTick | None:
        book = self._books.get(token_id)
        if not book:
            return None
        bids_raw = sorted(book["bids"].items(), key=lambda x: -x[0])
        asks_raw = sorted(book["asks"].items(), key=lambda x: x[0])
        bids = [CLOBLevel(price=p, size=s) for p, s in bids_raw if s > 0]
        asks = [CLOBLevel(price=p, size=s) for p, s in asks_raw if s > 0]
        if not bids or not asks:
            return None
        best_bid = bids[0].price
        best_ask = asks[0].price
        return CLOBTick(
            market_id=self._market_for_token.get(token_id, ""),
            token_id=token_id,
            ts_ms=ts_ms,
            received_ms=int(time.time() * 1000),
            bids=bids[:10],
            asks=asks[:10],
            best_bid=best_bid,
            best_ask=best_ask,
            mid=(best_bid + best_ask) / 2,
        )

    async def _handle_message(self, raw: str) -> None:
        now_ms = int(time.time() * 1000)
        events = json.loads(raw)
        if not isinstance(events, list):
            events = [events]

        for msg in events:
            msg_type = msg.get("event_type") or msg.get("type", "")
            token_id = msg.get("asset_id", "")

            if msg_type == "book":
                # Full book snapshot
                book = self._books.get(token_id)
                if book is None:
                    continue
                book["bids"] = {float(b["price"]): float(b["size"]) for b in msg.get("bids", [])}
                book["asks"] = {float(a["price"]): float(a["size"]) for a in msg.get("asks", [])}
                ts = int(msg.get("timestamp", now_ms))
                tick = self._build_tick(token_id, ts)
                if tick:
                    await self._dispatch_tick(tick)

            elif msg_type == "price_change":
                # Incremental update
                book = self._books.get(token_id)
                if book is None:
                    continue
                for change in msg.get("changes", []):
                    price = float(change["price"])
                    size = float(change["size"])
                    side = change["side"].upper()
                    side_key = "bids" if side == "BUY" else "asks"
                    if size == 0:
                        book[side_key].pop(price, None)
                    else:
                        book[side_key][price] = size
                ts = int(msg.get("timestamp", now_ms))
                tick = self._build_tick(token_id, ts)
                if tick:
                    await self._dispatch_tick(tick)

            elif msg_type == "last_trade_price":
                # Fill notification
                market_id = self._market_for_token.get(token_id, "")
                fill = FillEvent(
                    market_id=market_id,
                    token_id=token_id,
                    ts_ms=int(msg.get("timestamp", now_ms)),
                    received_ms=now_ms,
                    price=float(msg.get("price", 0)),
                    size=float(msg.get("size", 0)),
                    side=msg.get("side", ""),
                    taker_order_id=msg.get("taker_order_id", ""),
                    maker_order_id=msg.get("maker_order_id", ""),
                )
                await self._dispatch_fill(fill)

    async def _subscribe(self, ws) -> None:
        if not self._token_ids:
            return
        token_list = list(self._token_ids)
        # Subscribe to order book channel
        await ws.send(json.dumps({
            "type": "subscribe",
            "channel": "book",
            "assets_ids": token_list,
        }))
        # Subscribe to trade channel
        await ws.send(json.dumps({
            "type": "subscribe",
            "channel": "last_trade_price",
            "assets_ids": token_list,
        }))
        log.info("Subscribed to %d CLOB token(s)", len(token_list))

    async def _run_once(self) -> None:
        url = self._cfg.get("ws_url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
        log.info("Connecting to Polymarket CLOB WS")
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            ssl=create_ssl_context(),
        ) as ws:
            self._ws = ws
            await self._subscribe(ws)
            log.info("Polymarket CLOB websocket connected")
            async for raw in ws:
                await self._handle_message(raw)
                if not self._running:
                    break

    async def run(self) -> None:
        self._running = True
        delay = 5
        while self._running:
            try:
                await self._run_once()
            except Exception as exc:
                log.warning("CLOB feed disconnected: %s — reconnecting in %ss", exc, delay)
                await asyncio.sleep(delay)

    async def resubscribe(self) -> None:
        """Call when new markets are added while running."""
        if self._ws and not self._ws.closed:
            await self._subscribe(self._ws)

    def stop(self) -> None:
        self._running = False
