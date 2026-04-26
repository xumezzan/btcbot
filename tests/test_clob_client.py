import asyncio

from src.execution.clob_client import CLOBClient


def test_buy_order_budget_is_converted_to_shares_in_dryrun():
    async def run():
        client = CLOBClient({}, dry_run=True)
        order = await client.post_order("token", "market", "BUY", 0.25, 2.0)
        return order

    order = asyncio.run(run())

    assert order is not None
    assert order.price == 0.25
    assert order.size == 8.0


def test_sell_order_size_is_treated_as_shares_in_dryrun():
    async def run():
        client = CLOBClient({}, dry_run=True)
        order = await client.post_order("token", "market", "SELL", 0.25, 2.0)
        return order

    order = asyncio.run(run())

    assert order is not None
    assert order.price == 0.25
    assert order.size == 2.0
