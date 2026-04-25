"""
PnL reconciler.

Compares expected PnL (from our fill records + resolutions) against
actual wallet/on-chain balance changes. Flags discrepancies.

Also generates CSV export for tax accounting (Koinly/CoinTracker format).
"""
import csv
import io
import logging
import time

log = logging.getLogger(__name__)

DISCREPANCY_THRESHOLD = 0.10  # USDC — flag if diff > 10 cents


class Reconciler:
    def __init__(self, db_pool):
        self._db = db_pool
        self._expected_pnl: float = 0.0

    def record_expected_pnl(self, delta: float) -> None:
        self._expected_pnl += delta

    async def reconcile(self, actual_balance_usdc: float, starting_balance_usdc: float) -> dict:
        """
        Compare expected PnL to actual balance change.
        Returns reconciliation report dict.
        """
        actual_pnl = actual_balance_usdc - starting_balance_usdc
        discrepancy = abs(actual_pnl - self._expected_pnl)

        report = {
            "ts": time.time(),
            "expected_pnl": round(self._expected_pnl, 4),
            "actual_pnl": round(actual_pnl, 4),
            "discrepancy": round(discrepancy, 4),
            "ok": discrepancy <= DISCREPANCY_THRESHOLD,
        }

        if not report["ok"]:
            log.warning(
                "Reconciliation discrepancy: expected=%.4f actual=%.4f diff=%.4f",
                self._expected_pnl, actual_pnl, discrepancy,
            )
        else:
            log.info("Reconciliation OK: PnL=%.4f", actual_pnl)

        return report

    async def export_tax_csv(self, start_date: str, end_date: str) -> str:
        """
        Export all fills in Koinly-compatible CSV format.
        Returns CSV string.
        """
        try:
            rows = await self._db.fetch(
                "SELECT f.fill_id, f.market_id, f.token_side, f.price, f.size, "
                "       f.fee, f.ts_ms, m.symbol "
                "FROM fills f JOIN markets m ON f.market_id = m.condition_id "
                "WHERE f.ts_ms BETWEEN $1 AND $2 ORDER BY f.ts_ms",
                _date_to_ms(start_date),
                _date_to_ms(end_date, end_of_day=True),
            )
        except Exception as exc:
            log.error("Tax export DB query failed: %s", exc)
            return ""

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Koinly ID", "Date", "Sent Amount", "Sent Currency",
            "Received Amount", "Received Currency", "Fee Amount", "Fee Currency",
            "Net Worth Amount", "Net Worth Currency", "Label", "Description", "TxHash",
        ])

        for row in rows:
            dt = _ms_to_iso(row["ts_ms"])
            token = f"{row['symbol']}-{row['token_side']}"
            # BUY = sent USDC, received token
            # SELL = sent token, received USDC
            if row["token_side"] in ("UP", "DOWN"):
                sent_amt = row["price"] * row["size"]
                sent_cur = "USDC"
                recv_amt = row["size"]
                recv_cur = token
            else:
                sent_amt = row["size"]
                sent_cur = token
                recv_amt = row["price"] * row["size"]
                recv_cur = "USDC"

            writer.writerow([
                row["fill_id"], dt,
                round(sent_amt, 6), sent_cur,
                round(recv_amt, 6), recv_cur,
                round(row["fee"], 6), "MATIC",
                "", "", "trade",
                f"Polymarket {row['market_id'][:12]}",
                "",
            ])

        return output.getvalue()


def _date_to_ms(date_str: str, end_of_day: bool = False) -> int:
    from datetime import datetime, timezone, timedelta
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt += timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _ms_to_iso(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
