import pytest

from src.signals.value_signal import MarketPrices, OutcomeQuote, ValueSignalEngine


def test_buy_up_when_fair_exceeds_ask_by_edge():
    engine = ValueSignalEngine({"min_edge": 0.04, "cooldown_seconds": 0})
    decision = engine.decide(
        market_id="m1",
        up_token_id="up",
        down_token_id="down",
        fair_up=0.92,
        prices=MarketPrices(
            up=OutcomeQuote(bid=0.86, ask=0.87, ask_size=10),
            down=OutcomeQuote(bid=0.12, ask=0.14, ask_size=10),
        ),
        time_to_expiry_s=120,
        default_size_usdc=5,
    )

    assert decision.action == "BUY_UP"
    assert decision.token_id == "up"
    assert decision.price == 0.87


def test_buy_down_when_down_edge_is_best():
    engine = ValueSignalEngine({"min_edge": 0.04, "cooldown_seconds": 0})
    decision = engine.decide(
        market_id="m1",
        up_token_id="up",
        down_token_id="down",
        fair_up=0.30,
        prices=MarketPrices(
            up=OutcomeQuote(bid=0.76, ask=0.78, ask_size=10),
            down=OutcomeQuote(bid=0.58, ask=0.60, ask_size=10),
        ),
        time_to_expiry_s=120,
        default_size_usdc=5,
    )

    assert decision.action == "BUY_DOWN"
    assert decision.token_id == "down"
    assert decision.edge == pytest.approx(0.10)


def test_skip_when_edge_is_too_small():
    engine = ValueSignalEngine({"min_edge": 0.04, "cooldown_seconds": 0})
    decision = engine.decide(
        market_id="m1",
        up_token_id="up",
        down_token_id="down",
        fair_up=0.89,
        prices=MarketPrices(
            up=OutcomeQuote(bid=0.86, ask=0.87, ask_size=10),
            down=OutcomeQuote(bid=0.12, ask=0.14, ask_size=10),
        ),
        time_to_expiry_s=120,
        default_size_usdc=5,
    )

    assert decision.action == "SKIP"
    assert "below min" in decision.reason


def test_mark_order_sent_blocks_second_signal_for_market():
    engine = ValueSignalEngine({"min_edge": 0.04, "cooldown_seconds": 0})
    engine.mark_order_sent("m1")

    decision = engine.decide(
        market_id="m1",
        up_token_id="up",
        down_token_id="down",
        fair_up=0.95,
        prices=MarketPrices(
            up=OutcomeQuote(bid=0.86, ask=0.87, ask_size=10),
            down=OutcomeQuote(bid=0.12, ask=0.14, ask_size=10),
        ),
        time_to_expiry_s=120,
        default_size_usdc=5,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "already traded market"
