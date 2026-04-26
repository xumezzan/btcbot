import asyncio

from src.execution.clob_client import Fill, CLOBClient


def test_get_user_fills_returns_dryrun_fills():
    async def run():
        client = CLOBClient({}, dry_run=True)
        client._dry_run_fills.append(Fill(
            fill_id="f1",
            order_id="o1",
            market_id="m1",
            token_id="t1",
            side="BUY",
            price=0.5,
            size=4.0,
            fee=0.0,
            ts_ms=123,
        ))
        return await client.get_user_fills()

    fills = asyncio.run(run())

    assert len(fills) == 1
    assert fills[0].fill_id == "f1"
    assert fills[0].size == 4.0
