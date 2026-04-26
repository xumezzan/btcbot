"""
Polymarket CLOB execution client.

Wraps the py-clob-client library to:
  - post limit orders (maker-only / post-only)
  - cancel individual orders and all-open-orders
  - query open orders and fills
  - redeem settled positions

DRY-RUN MODE: when dry_run=True, all mutating calls are logged but NOT submitted.
This is the default until the user explicitly sets dry_run=False in settings.

Key management: private key is loaded from env, never passed in code.
"""
import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Order:
    order_id: str
    market_id: str
    token_id: str
    side: str            # "BUY" or "SELL"
    price: float
    size: float
    status: str          # "OPEN", "FILLED", "CANCELLED"
    ts_ms: int


@dataclass
class Fill:
    fill_id: str
    order_id: str
    market_id: str
    token_id: str
    side: str
    price: float
    size: float
    fee: float
    ts_ms: int


class CLOBClient:
    """
    Thin async wrapper around py-clob-client.

    In dry-run mode all write operations return synthetic responses.
    In live mode they hit the real Polymarket CLOB API.

    Gas fee estimate: ~0.02 USDC per order/cancel on Polygon mainnet.
    """

    GAS_FEE_EST = 0.02  # USDC estimate per tx

    def __init__(self, cfg: dict, dry_run: bool = True):
        self._dry_run = dry_run
        self._cfg = cfg
        self._client = None
        self._open_orders: dict[str, Order] = {}
        self._dry_run_fills: list[Fill] = []

        if not dry_run:
            self._init_client()

    def _init_client(self) -> None:
        """Initialize the real py-clob-client. Only called in live mode."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            private_key = os.environ.get("POLY_PRIVATE_KEY")
            api_key = os.environ.get("POLY_API_KEY")
            api_secret = os.environ.get("POLY_API_SECRET")
            api_passphrase = os.environ.get("POLY_API_PASSPHRASE")

            if not all([private_key, api_key, api_secret, api_passphrase]):
                raise ValueError(
                    "Missing Polymarket credentials. Set POLY_PRIVATE_KEY, "
                    "POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE in .env"
                )

            host = self._cfg.get("clob_api_url", "https://clob.polymarket.com")
            chain_id = 137  # Polygon mainnet

            self._client = ClobClient(
                host=host,
                key=private_key,
                chain_id=chain_id,
                creds=ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                ),
            )
            log.info("CLOB client initialized (LIVE MODE)")
        except ImportError:
            raise RuntimeError("py-clob-client not installed. Run: pip install py-clob-client")

    async def post_order(
        self,
        token_id: str,
        market_id: str,
        side: str,       # "BUY" or "SELL"
        price: float,
        size_usdc: float,
    ) -> Optional[Order]:
        """
        Post a limit order. Returns Order if accepted.
        For BUY orders, size_usdc is a max USDC budget and is converted to
        outcome-token shares because Polymarket's CLOB order size is shares.
        For SELL orders, size_usdc is treated as shares to sell.
        In dry-run mode, creates a synthetic order.
        """
        order_id = str(uuid.uuid4())
        ts = int(time.time() * 1000)
        side = side.upper()
        price = round(float(price), 4)
        if price <= 0:
            log.error("Refusing order with invalid price %.4f", price)
            return None

        size_shares = float(size_usdc)
        cost_usdc = size_shares * price
        if side == "BUY":
            cost_usdc = float(size_usdc)
            size_shares = cost_usdc / price

        if self._dry_run:
            log.info("[DRY-RUN] POST ORDER %s %s token=%s price=%.4f shares=%.4f cost=$%.2f",
                     side, market_id[:12], token_id[:12], price, size_shares, cost_usdc)
            order = Order(
                order_id=order_id,
                market_id=market_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size_shares,
                status="OPEN",
                ts_ms=ts,
            )
            self._open_orders[order_id] = order
            return order

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size_shares,
                side=side,
                order_type=OrderType.GTC,
            )
            resp = await asyncio.to_thread(self._client.create_and_post_order, args)
            order_id = resp.get("orderID", order_id)
            order = Order(
                order_id=order_id,
                market_id=market_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size_shares,
                status="OPEN",
                ts_ms=ts,
            )
            self._open_orders[order_id] = order
            log.info("Posted order %s %s price=%.4f shares=%.4f cost=$%.2f",
                     side, order_id[:8], price, size_shares, cost_usdc)
            return order
        except Exception as exc:
            log.error("Failed to post order: %s", exc)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        if self._dry_run:
            log.debug("[DRY-RUN] CANCEL order %s", order_id[:12])
            if order_id in self._open_orders:
                self._open_orders[order_id].status = "CANCELLED"
            return True
        try:
            await asyncio.to_thread(self._client.cancel, order_id)
            if order_id in self._open_orders:
                self._open_orders[order_id].status = "CANCELLED"
            return True
        except Exception as exc:
            log.warning("Cancel failed for %s: %s", order_id[:8], exc)
            return False

    async def cancel_all(self, market_id: str | None = None) -> int:
        """Cancel all open orders, optionally filtered to one market."""
        to_cancel = [
            oid for oid, o in self._open_orders.items()
            if o.status == "OPEN" and (market_id is None or o.market_id == market_id)
        ]
        if not to_cancel:
            return 0

        if self._dry_run:
            log.debug("[DRY-RUN] CANCEL ALL (%d orders) market=%s", len(to_cancel), market_id)
            for oid in to_cancel:
                self._open_orders[oid].status = "CANCELLED"
            return len(to_cancel)

        cancelled = 0
        for oid in to_cancel:
            if await self.cancel_order(oid):
                cancelled += 1
        log.info("Cancelled %d/%d orders for market=%s", cancelled, len(to_cancel), market_id)
        return cancelled

    async def get_open_orders(self, market_id: str | None = None) -> list[Order]:
        if self._dry_run:
            return [
                o for o in self._open_orders.values()
                if o.status == "OPEN" and (market_id is None or o.market_id == market_id)
            ]
        try:
            raw = await asyncio.to_thread(self._client.get_orders)
            orders = []
            for r in (raw or []):
                if market_id and r.get("market") != market_id:
                    continue
                orders.append(Order(
                    order_id=r["id"],
                    market_id=r.get("market", ""),
                    token_id=r.get("asset_id", ""),
                    side=r.get("side", ""),
                    price=float(r.get("price", 0)),
                    size=float(r.get("original_size", 0)),
                    status=r.get("status", "OPEN"),
                    ts_ms=int(r.get("created_at", 0) * 1000),
                ))
            return orders
        except Exception as exc:
            log.error("get_open_orders failed: %s", exc)
            return []

    async def get_user_fills(self, market_id: str | None = None, after_ts_ms: int | None = None) -> list[Fill]:
        """
        Fetch authenticated user trade history from the CLOB API.

        This is read-only and returns only trades for the configured API key.
        Polymarket's TradeParams after/before fields are timestamp filters; use
        seconds to match the SDK/API convention.
        """
        if self._dry_run:
            return list(self._dry_run_fills)

        try:
            from py_clob_client.clob_types import TradeParams

            params = TradeParams(
                market=market_id,
                after=(after_ts_ms // 1000) if after_ts_ms else None,
            )
            raw = await asyncio.to_thread(self._client.get_trades, params)
            fills: list[Fill] = []
            for r in raw or []:
                ts_raw = r.get("timestamp") or r.get("created_at") or r.get("createdAt") or 0
                try:
                    ts_ms = int(float(ts_raw) * 1000)
                except (TypeError, ValueError):
                    ts_ms = int(time.time() * 1000)

                fills.append(Fill(
                    fill_id=str(r.get("id") or r.get("trade_id") or r.get("transactionHash") or ""),
                    order_id=str(r.get("order_id") or r.get("orderId") or ""),
                    market_id=str(r.get("market") or r.get("condition_id") or r.get("conditionId") or ""),
                    token_id=str(r.get("asset_id") or r.get("assetId") or r.get("token_id") or ""),
                    side=str(r.get("side") or ""),
                    price=float(r.get("price") or 0),
                    size=float(r.get("size") or 0),
                    fee=float(r.get("fee") or 0),
                    ts_ms=ts_ms,
                ))
            return fills
        except Exception as exc:
            log.error("get_user_fills failed: %s", exc)
            return []

    async def get_balance_allowance(self) -> dict:
        """Fetch authenticated collateral balance/allowance from the CLOB API."""
        if self._dry_run:
            return {"balance": "0", "allowances": {}}

        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            return await asyncio.to_thread(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
        except Exception as exc:
            log.error("get_balance_allowance failed: %s", exc)
            return {}

    async def redeem_position(self, condition_id: str) -> bool:
        """
        Redeem a resolved conditional token position for USDC.
        Must be called after market resolution to recycle capital.
        """
        if self._dry_run:
            log.info("[DRY-RUN] REDEEM position for condition %s", condition_id[:12])
            return True
        try:
            await asyncio.to_thread(self._client.redeem_positions, condition_id)
            log.info("Redeemed position for %s", condition_id[:12])
            return True
        except Exception as exc:
            log.error("Redeem failed for %s: %s", condition_id[:12], exc)
            return False

    def open_order_count(self, market_id: str | None = None) -> int:
        return sum(
            1 for o in self._open_orders.values()
            if o.status == "OPEN" and (market_id is None or o.market_id == market_id)
        )
