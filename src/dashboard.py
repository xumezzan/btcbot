"""
Terminal dashboard using Rich.

Shows live bot state: spot prices, active markets, PnL, inventory,
risk state, fill stats, adverse selection rate.
Press Ctrl+C to stop.
"""
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class Dashboard:
    def __init__(self):
        self._console = Console()
        self._state: dict = {
            "spot": {},          # symbol -> price
            "markets": [],       # list of market dicts
            "pnl": 0.0,
            "daily_loss": 0.0,
            "fills_today": 0,
            "adverse_rate": None,
            "risk_killed": False,
            "kill_reason": "",
            "inventory": [],     # list of position dicts
            "mode": "collect",
            "uptime_s": 0,
        }
        self._start_ts = time.time()

    def update(self, **kwargs) -> None:
        self._state.update(kwargs)
        self._state["uptime_s"] = int(time.time() - self._start_ts)

    def _render(self) -> Layout:
        s = self._state
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )

        # Header
        mode_color = {"collect": "blue", "dryrun": "yellow", "live": "green", "backtest": "magenta"}.get(s["mode"], "white")
        kill_txt = f" [red]⚠ KILLED: {s['kill_reason']}[/red]" if s["risk_killed"] else ""
        layout["header"].update(Panel(
            f"[bold {mode_color}]btcbot [{s['mode'].upper()}][/bold {mode_color}]"
            f"  uptime {_fmt_seconds(s['uptime_s'])}"
            f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            + kill_txt,
            style="bold",
        ))

        # Left: spot + markets
        left_content = Table.grid(expand=True)
        left_content.add_column()

        # Spot prices
        spot_tbl = Table(title="Spot Prices", box=None, padding=(0, 1))
        spot_tbl.add_column("Symbol")
        spot_tbl.add_column("Price", justify="right")
        for sym, price in s["spot"].items():
            spot_tbl.add_row(sym, f"${price:,.2f}")
        left_content.add_row(spot_tbl)

        # Active markets
        mkt_tbl = Table(title=f"Active Markets ({len(s['markets'])})", box=None, padding=(0, 1))
        mkt_tbl.add_column("Symbol")
        mkt_tbl.add_column("Duration")
        mkt_tbl.add_column("Stage")
        mkt_tbl.add_column("TTX")
        mkt_tbl.add_column("Fair UP")
        for m in s["markets"][:10]:
            stage_color = {"ACTIVE": "green", "NEW": "blue", "NEAR_EXPIRY": "yellow",
                           "DANGER": "red", "EXPIRED": "dim"}.get(m.get("stage", ""), "white")
            mkt_tbl.add_row(
                m.get("symbol", ""),
                f"{m.get('duration_minutes', '')}m",
                Text(m.get("stage", ""), style=stage_color),
                f"{m.get('tte_s', 0):.0f}s",
                f"{m.get('fair_up', 0):.3f}" if m.get("fair_up") else "-",
            )
        left_content.add_row(mkt_tbl)
        layout["left"].update(Panel(left_content, title="Markets"))

        # Right: PnL + inventory + risk
        right_content = Table.grid(expand=True)
        right_content.add_column()

        # PnL summary
        pnl_color = "green" if s["pnl"] >= 0 else "red"
        pnl_tbl = Table(title="PnL", box=None, padding=(0, 1))
        pnl_tbl.add_column("Metric")
        pnl_tbl.add_column("Value", justify="right")
        pnl_tbl.add_row("Total PnL", Text(f"${s['pnl']:+.4f}", style=pnl_color))
        pnl_tbl.add_row("Daily Loss", f"${s['daily_loss']:.4f}")
        pnl_tbl.add_row("Fills Today", str(s["fills_today"]))
        adv = s.get("adverse_rate")
        adv_txt = f"{adv:.1%}" if adv is not None else "N/A"
        adv_color = "red" if (adv or 0) > 0.45 else "green"
        pnl_tbl.add_row("Adverse Sel.", Text(adv_txt, style=adv_color))
        right_content.add_row(pnl_tbl)

        # Inventory
        inv_tbl = Table(title="Inventory", box=None, padding=(0, 1))
        inv_tbl.add_column("Market")
        inv_tbl.add_column("UP", justify="right")
        inv_tbl.add_column("DOWN", justify="right")
        inv_tbl.add_column("Delta", justify="right")
        for pos in s["inventory"][:8]:
            delta = pos.get("net_delta", 0)
            delta_color = "green" if delta > 0 else "red" if delta < 0 else "white"
            inv_tbl.add_row(
                pos.get("market_id", "")[:12],
                f"{pos.get('up_shares', 0):.1f}",
                f"{pos.get('down_shares', 0):.1f}",
                Text(f"{delta:+.2f}", style=delta_color),
            )
        right_content.add_row(inv_tbl)
        layout["right"].update(Panel(right_content, title="Risk & PnL"))

        # Footer
        layout["footer"].update(Panel("Press Ctrl+C to stop", style="dim"))
        return layout

    def run_blocking(self, get_state_fn=None) -> None:
        with Live(self._render(), console=self._console, refresh_per_second=1) as live:
            try:
                while True:
                    if get_state_fn:
                        self.update(**get_state_fn())
                    live.update(self._render())
                    time.sleep(1)
            except KeyboardInterrupt:
                pass


def _fmt_seconds(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"
