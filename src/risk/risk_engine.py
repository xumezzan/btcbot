"""
Risk engine and kill switch.

Hard limits:
  - Per-market max exposure
  - Total portfolio exposure
  - Daily loss limit (resets at UTC midnight)
  - Adverse selection kill switch
  - Cross-asset net delta warning

When the kill switch triggers, all quoting stops and a notification is sent.
The bot must be manually restarted after a kill switch event (prevents loops).
"""
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto

log = logging.getLogger(__name__)


class KillReason(Enum):
    DAILY_LOSS = auto()
    ADVERSE_SELECTION = auto()
    TOTAL_EXPOSURE = auto()
    API_ERROR = auto()
    MANUAL = auto()


@dataclass
class RiskState:
    is_killed: bool = False
    kill_reason: KillReason | None = None
    kill_ts: float = 0.0
    daily_loss: float = 0.0
    daily_reset_ts: float = 0.0  # UTC midnight timestamp
    total_realized_pnl: float = 0.0
    warnings: list[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, cfg: dict, alert_fn=None):
        self._max_order = cfg.get("max_order_size_usdc", 5.0)
        self._max_market_exposure = cfg.get("max_market_exposure_usdc", 25.0)
        self._max_total_exposure = cfg.get("max_total_exposure_usdc", 100.0)
        self._max_daily_loss = cfg.get("max_daily_loss_usdc", 50.0)
        self._max_net_delta = cfg.get("max_net_delta_usdc", 20.0)
        self._max_adverse_rate = cfg.get("max_adverse_selection_rate", 0.50)
        self._alert_fn = alert_fn  # async callable(message: str)
        self._state = RiskState(daily_reset_ts=self._next_midnight())

    @staticmethod
    def _next_midnight() -> float:
        now = time.time()
        return now - (now % 86400) + 86400  # next UTC midnight

    def _reset_daily_if_needed(self) -> None:
        if time.time() >= self._state.daily_reset_ts:
            log.info("Daily loss counter reset")
            self._state.daily_loss = 0.0
            self._state.daily_reset_ts = self._next_midnight()

    def is_alive(self) -> bool:
        return not self._state.is_killed

    def record_pnl(self, delta_pnl: float) -> None:
        self._reset_daily_if_needed()
        self._state.total_realized_pnl += delta_pnl
        if delta_pnl < 0:
            self._state.daily_loss += abs(delta_pnl)
            if self._state.daily_loss >= self._max_daily_loss:
                self._trigger_kill(KillReason.DAILY_LOSS,
                                   f"Daily loss ${self._state.daily_loss:.2f} >= limit ${self._max_daily_loss}")

    def check_order_size(self, size_usdc: float) -> bool:
        return size_usdc <= self._max_order

    def check_market_exposure(self, market_exposure_usdc: float) -> bool:
        return market_exposure_usdc <= self._max_market_exposure

    def check_total_exposure(self, total_exposure_usdc: float) -> bool:
        if total_exposure_usdc > self._max_total_exposure:
            self._trigger_kill(KillReason.TOTAL_EXPOSURE,
                               f"Total exposure ${total_exposure_usdc:.2f} > limit ${self._max_total_exposure}")
            return False
        return True

    def check_adverse_selection(self, rate: float | None) -> bool:
        if rate is None:
            return True  # not enough data yet
        if rate > self._max_adverse_rate:
            self._trigger_kill(KillReason.ADVERSE_SELECTION,
                               f"Adverse selection {rate:.1%} > limit {self._max_adverse_rate:.1%}")
            return False
        return True

    def check_net_delta(self, net_delta_usdc: float) -> None:
        if abs(net_delta_usdc) > self._max_net_delta:
            msg = f"Net delta ${net_delta_usdc:+.2f} exceeds warning threshold ${self._max_net_delta}"
            log.warning(msg)
            if msg not in self._state.warnings:
                self._state.warnings.append(msg)

    def api_error_kill(self, reason: str) -> None:
        self._trigger_kill(KillReason.API_ERROR, reason)

    def manual_kill(self) -> None:
        self._trigger_kill(KillReason.MANUAL, "Manual kill switch activated")

    def _trigger_kill(self, reason: KillReason, message: str) -> None:
        if self._state.is_killed:
            return
        self._state.is_killed = True
        self._state.kill_reason = reason
        self._state.kill_ts = time.time()
        log.critical("KILL SWITCH: %s — %s", reason.name, message)
        if self._alert_fn:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._alert_fn(f"🚨 KILL SWITCH [{reason.name}]: {message}"))
            except Exception:
                pass

    def get_state(self) -> RiskState:
        return self._state

    def clamp_order_size(self, desired_usdc: float) -> float:
        return min(desired_usdc, self._max_order)
