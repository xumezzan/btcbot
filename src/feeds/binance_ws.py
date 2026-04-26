"""
Binance L2 orderbook + trade feed via websocket.

Maintains a live local copy of the top-N order book levels and computes
microprice (volume-weighted mid). Publishes SpotTick events to all
registered callbacks and to the database.
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
class SpotTick:
    symbol: str          # e.g. "BTCUSDT"
    ts_ms: int           # exchange event time (ms)
    received_ms: int     # local receive time (ms)
    bid1: float
    bid1_qty: float
    ask1: float
    ask1_qty: float
    microprice: float    # volume-weighted mid
    last_trade: float    # last trade price (from @trade stream)


@dataclass
class _BookState:
    bids: list[tuple[float, float]] = field(default_factory=list)  # [(price, qty), ...]
    asks: list[tuple[float, float]] = field(default_factory=list)
    last_trade: float = 0.0


def _microprice(bids: list, asks: list) -> float:
    """Weighted mid: bid * ask_qty + ask * bid_qty / (bid_qty + ask_qty)."""
    if not bids or not asks:
        return 0.0
    b, bq = bids[0]
    a, aq = asks[0]
    if bq + aq == 0:
        return (b + a) / 2
    return (b * aq + a * bq) / (bq + aq)


class BinanceFeed:
    """
    Subscribes to <symbol>@depth5@100ms and <symbol>@trade for each symbol.
    Calls registered callbacks with SpotTick on every update.
    Auto-reconnects on disconnect.
    """

    def __init__(self, symbols: list[str], cfg: dict):
        self._symbols = [s.lower() for s in symbols]
        self._cfg = cfg
        self._callbacks: list[Callable[[SpotTick], Awaitable[None]]] = []
        self._book: dict[str, _BookState] = {s: _BookState() for s in self._symbols}
        self._running = False

    def add_callback(self, fn: Callable[[SpotTick], Awaitable[None]]) -> None:
        self._callbacks.append(fn)

    async def _dispatch(self, tick: SpotTick) -> None:
        for cb in self._callbacks:
            try:
                await cb(tick)
            except Exception:
                log.exception("Callback error for %s", tick.symbol)

    def _build_stream_url(self) -> str:
        base = self._cfg.get("ws_base", "wss://stream.binance.com:9443/stream")
        streams = []
        for s in self._symbols:
            depth = self._cfg.get("orderbook_depth", 5)
            streams.append(f"{s}@depth{depth}@100ms")
            streams.append(f"{s}@trade")
        return f"{base}?streams={'/'.join(streams)}"

    async def _handle_message(self, raw: str) -> None:
        msg = json.loads(raw)
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        symbol_raw = data.get("s", "").lower()
        if not symbol_raw:
            return

        state = self._book.get(symbol_raw)
        if state is None:
            return

        now_ms = int(time.time() * 1000)

        if "@depth" in stream:
            bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
            # Filter out zero-qty levels
            state.bids = [(p, q) for p, q in bids if q > 0]
            state.asks = [(p, q) for p, q in asks if q > 0]

            if not state.bids or not state.asks:
                return

            mp = _microprice(state.bids, state.asks)
            tick = SpotTick(
                symbol=symbol_raw.upper(),
                ts_ms=data.get("T", now_ms),
                received_ms=now_ms,
                bid1=state.bids[0][0],
                bid1_qty=state.bids[0][1],
                ask1=state.asks[0][0],
                ask1_qty=state.asks[0][1],
                microprice=mp,
                last_trade=state.last_trade,
            )
            await self._dispatch(tick)

        elif "@trade" in stream:
            price = float(data.get("p", 0))
            state.last_trade = price

    async def _run_once(self) -> None:
        url = self._build_stream_url()
        delay = self._cfg.get("reconnect_delay_seconds", 5)
        log.info("Connecting to Binance: %s", url)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            ssl=create_ssl_context(),
        ) as ws:
            log.info("Binance websocket connected")
            async for raw in ws:
                await self._handle_message(raw)
                if not self._running:
                    break

    async def run(self) -> None:
        self._running = True
        delay = self._cfg.get("reconnect_delay_seconds", 5)
        while self._running:
            try:
                await self._run_once()
            except Exception as exc:
                log.warning("Binance feed disconnected: %s — reconnecting in %ss", exc, delay)
                await asyncio.sleep(delay)

    def stop(self) -> None:
        self._running = False
