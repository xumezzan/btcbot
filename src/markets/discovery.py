"""
Market discovery via Polymarket Gamma API.

Polls for active Bitcoin/Ethereum "Up or Down" prediction markets,
filters by volume and duration, and stores metadata to the database.
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Gamma API query tags for Up/Down markets
UP_DOWN_TAGS = ["bitcoin-up-or-down", "ethereum-up-or-down", "crypto-up-or-down"]

CHAINLINK_STREAM_RE = re.compile(r"data\.chain\.link/streams/([a-z0-9-]+)", re.IGNORECASE)
PYTH_FEED_RE = re.compile(r"pythdata\.app/explore/([^)\s,]+)", re.IGNORECASE)


@dataclass
class MarketInfo:
    condition_id: str       # Polymarket market ID
    question: str           # e.g. "Will BTC be up at 2:00 PM?"
    symbol: str             # BTC / ETH / SOL etc.
    start_price: float      # strike price / start price
    up_token_id: str        # YES/UP conditional token ID
    down_token_id: str      # NO/DOWN conditional token ID
    start_time: int         # unix ts ms
    end_time: int           # unix ts ms
    duration_minutes: int
    volume_24h: float
    resolution_source: str  # e.g. "pyth", "chainlink", "manual"
    oracle_feed_id: str     # specific feed identifier for resolution
    active: bool


def _json_list(value) -> list:
    """Gamma often returns JSON arrays encoded as strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _extract_tokens(m: dict) -> tuple[Optional[dict], Optional[dict]]:
    tokens = m.get("tokens") or []
    if isinstance(tokens, str):
        tokens = _json_list(tokens)

    if tokens and isinstance(tokens[0], dict):
        up_token, down_token = None, None
        for t in tokens:
            outcome = t.get("outcome", "").lower()
            if outcome in ("up", "yes"):
                up_token = t
            elif outcome in ("down", "no"):
                down_token = t

        if up_token and down_token:
            return up_token, down_token
        if len(tokens) >= 2:
            return tokens[0], tokens[1]

    outcomes = [str(o).lower() for o in _json_list(m.get("outcomes"))]
    token_ids = [str(t) for t in _json_list(m.get("clobTokenIds"))]
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None, None

    up_token, down_token = None, None
    for outcome, token_id in zip(outcomes, token_ids):
        token = {"outcome": outcome, "token_id": token_id, "tokenId": token_id}
        if outcome in ("up", "yes"):
            up_token = token
        elif outcome in ("down", "no"):
            down_token = token

    if up_token and down_token:
        return up_token, down_token
    return {"token_id": token_ids[0]}, {"token_id": token_ids[1]}


def _parse_resolution(rules: str, symbol: str) -> tuple[str, str]:
    rules_l = rules.lower()
    chainlink_match = CHAINLINK_STREAM_RE.search(rules)
    if "chainlink" in rules_l:
        stream = chainlink_match.group(1).lower() if chainlink_match else f"{symbol.lower()}-usd"
        return "chainlink", f"chainlink:{stream}"

    if "binance" in rules_l:
        return "binance", f"binance:{symbol}USDT"

    pyth_match = PYTH_FEED_RE.search(rules)
    if "pyth" in rules_l:
        feed = pyth_match.group(1) if pyth_match else symbol
        return "pyth", f"pyth:{feed}"

    if "coinbase" in rules_l:
        return "coinbase", f"coinbase:{symbol}-USD"

    return "unknown", ""


def _parse_market(m: dict) -> Optional[MarketInfo]:
    """Parse a Gamma API market object into MarketInfo. Returns None if not Up/Down."""
    question_raw = m.get("question", "")
    question = question_raw.lower()
    slug = str(m.get("slug", "")).lower()
    # Only handle Up or Down markets
    if "up or down" not in question and "up-or-down" not in question and "updown" not in slug:
        return None

    # Determine asset symbol
    symbol = "BTC"
    lookup_text = f"{question} {slug}"
    for asset in ["bitcoin", "btc", "ethereum", "eth", "sol", "xrp", "doge"]:
        if asset in lookup_text:
            if asset == "bitcoin":
                symbol = "BTC"
            elif asset == "ethereum":
                symbol = "ETH"
            else:
                symbol = asset.upper()
            break

    # Parse tokens: Polymarket markets have exactly 2 outcome tokens
    up_token, down_token = _extract_tokens(m)
    if not up_token or not down_token:
        return None

    start_time_ms = int(float(m.get("startDateIso", 0) or 0) * 1000) if "startDateIso" in m else 0
    end_time_ms = int(float(m.get("endDateIso", 0) or 0) * 1000) if "endDateIso" in m else 0

    # Try to parse from endDate timestamp fields
    start_ts = m.get("startDate")
    end_ts = m.get("endDate")
    if start_ts:
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(str(start_ts).replace("Z", "+00:00"))
            start_time_ms = int(dt.timestamp() * 1000)
        except Exception:
            pass
    if end_ts:
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(str(end_ts).replace("Z", "+00:00"))
            end_time_ms = int(dt.timestamp() * 1000)
        except Exception:
            pass

    duration_ms = end_time_ms - start_time_ms
    duration_minutes = max(1, duration_ms // 60000)

    # Resolution source — check description/rules field
    rules = m.get("description", "") or m.get("resolutionSource", "") or ""
    resolution_source, oracle_feed_id = _parse_resolution(rules, symbol)

    return MarketInfo(
        condition_id=m.get("conditionId", m.get("id", "")),
        question=m.get("question", ""),
        symbol=symbol,
        start_price=float(m.get("startPrice") or 0),
        up_token_id=up_token.get("token_id", up_token.get("tokenId", "")),
        down_token_id=down_token.get("token_id", down_token.get("tokenId", "")),
        start_time=start_time_ms,
        end_time=end_time_ms,
        duration_minutes=duration_minutes,
        volume_24h=float(m.get("volume24hr") or m.get("volume") or 0),
        resolution_source=resolution_source,
        oracle_feed_id=oracle_feed_id,
        active=m.get("active", True) and not m.get("closed", False),
    )


class MarketDiscovery:
    """
    Polls Gamma API periodically for active Up/Down markets.
    Filters by allowed durations and minimum volume.
    Calls on_new_market / on_expired_market callbacks.
    """

    def __init__(self, cfg: dict, allowed_durations: list[int], min_volume: float):
        self._cfg = cfg
        self._allowed_durations = set(allowed_durations)
        self._min_volume = min_volume
        self._known: dict[str, MarketInfo] = {}
        self._new_callbacks: list = []
        self._expired_callbacks: list = []
        self._refresh_interval = cfg.get("market_refresh_interval_seconds", 60)

    def on_new_market(self, fn) -> None:
        self._new_callbacks.append(fn)

    def on_expired_market(self, fn) -> None:
        self._expired_callbacks.append(fn)

    async def _fetch_markets(self, session: "aiohttp.ClientSession") -> list[MarketInfo]:
        import aiohttp

        gamma = self._cfg.get("gamma_api_url", GAMMA_API)
        expected_oracle = str(self._cfg.get("expected_oracle", "any")).lower()
        params = {
            "active": "true",
            "closed": "false",
            "tag": "crypto",
            "limit": 200,
        }
        results = []
        try:
            async with session.get(f"{gamma}/markets", params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                markets_raw = data if isinstance(data, list) else data.get("data", [])
                for m in markets_raw:
                    info = _parse_market(m)
                    if info is None:
                        continue
                    if info.duration_minutes not in self._allowed_durations:
                        continue
                    if expected_oracle != "any" and info.resolution_source != expected_oracle:
                        log.debug("Skipping %s: oracle=%s expected=%s",
                                  info.question[:40], info.resolution_source, expected_oracle)
                        continue
                    if info.volume_24h < self._min_volume:
                        continue
                    results.append(info)
        except Exception as exc:
            log.warning("Gamma API fetch failed: %s", exc)
        return results

    async def _refresh(self, session: "aiohttp.ClientSession") -> None:
        markets = await self._fetch_markets(session)
        current_ids = {m.condition_id for m in markets}
        known_ids = set(self._known.keys())

        # New markets
        for m in markets:
            if m.condition_id not in known_ids:
                log.info("New market: %s %s %dmin vol=%.0f oracle=%s",
                         m.symbol, m.question[:40], m.duration_minutes,
                         m.volume_24h, m.resolution_source)
                self._known[m.condition_id] = m
                for cb in self._new_callbacks:
                    try:
                        await cb(m)
                    except Exception:
                        log.exception("on_new_market callback error")

        # Expired markets
        for cid in known_ids - current_ids:
            m = self._known.pop(cid)
            log.info("Market expired: %s %s", m.symbol, m.question[:40])
            for cb in self._expired_callbacks:
                try:
                    await cb(m)
                except Exception:
                    log.exception("on_expired_market callback error")

        log.debug("Active markets: %d", len(self._known))

    async def run(self) -> None:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            while True:
                await self._refresh(session)
                await asyncio.sleep(self._refresh_interval)

    def get_active_markets(self) -> list[MarketInfo]:
        return list(self._known.values())
