import asyncio

from src.execution.clob_client import CLOBClient


def test_get_balance_allowance_returns_empty_dryrun_state():
    async def run():
        client = CLOBClient({}, dry_run=True)
        return await client.get_balance_allowance()

    result = asyncio.run(run())

    assert result["balance"] == "0"
