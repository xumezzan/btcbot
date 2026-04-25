from src.markets.discovery import _parse_market


def test_parse_gamma_updown_market_with_clob_token_ids():
    market = {
        "question": "BTC Up or Down 15m",
        "slug": "btc-updown-15m-1777102200",
        "description": (
            'This market will resolve to "Up" if the Bitcoin price at the end '
            "is greater than or equal to the price at the beginning. "
            "The resolution source for this market is information from Chainlink, "
            "specifically the BTC/USD data stream available at "
            "https://data.chain.link/streams/btc-usd."
        ),
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "conditionId": "0xabc",
        "startDate": "2026-04-25T15:30:00Z",
        "endDate": "2026-04-25T15:45:00Z",
        "volume24hr": "1000",
        "active": True,
        "closed": False,
    }

    parsed = _parse_market(market)

    assert parsed is not None
    assert parsed.symbol == "BTC"
    assert parsed.up_token_id == "111"
    assert parsed.down_token_id == "222"
    assert parsed.duration_minutes == 15
    assert parsed.resolution_source == "chainlink"
    assert parsed.oracle_feed_id == "chainlink:btc-usd"


def test_parse_hourly_binance_market_resolution():
    market = {
        "question": "Bitcoin Up or Down - April 25, 5AM ET",
        "slug": "bitcoin-up-or-down-april-25-2026-5am-et",
        "description": (
            "The resolution source for this market is information from Binance, "
            "specifically the BTC/USDT pair."
        ),
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["333", "444"]',
        "conditionId": "0xdef",
        "startDate": "2026-04-25T09:00:00Z",
        "endDate": "2026-04-25T10:00:00Z",
        "volume24hr": "1000",
        "active": True,
        "closed": False,
    }

    parsed = _parse_market(market)

    assert parsed is not None
    assert parsed.symbol == "BTC"
    assert parsed.duration_minutes == 60
    assert parsed.resolution_source == "binance"
    assert parsed.oracle_feed_id == "binance:BTCUSDT"
