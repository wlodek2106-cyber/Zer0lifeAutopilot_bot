"""Translate validated dashboard preferences into isolated research components."""
from .paper import PaperBroker
from .risk import RiskLimits
from .settings import validate_settings


def build_risk_limits(payload):
    settings = validate_settings(payload)
    return RiskLimits(
        max_order_fraction=settings["orderRisk"] / 100.0,
        max_open_positions=settings["maxPositions"],
        max_daily_loss_fraction=settings["dailyLoss"] / 100.0,
        min_confidence=settings["confidence"] / 100.0,
    )


def build_paper_broker(payload):
    settings = validate_settings(payload)
    return PaperBroker(
        starting_cash=settings["capital"],
        fee_bps=settings["fee"],
        slippage_bps=settings["slippage"],
    )


def build_backtest_options(payload):
    settings = validate_settings(payload)
    return {
        "starting_cash": settings["capital"],
        "fee_bps": settings["fee"],
        "slippage_bps": settings["slippage"],
        "order_fraction": settings["orderRisk"] / 100.0,
        "min_confidence": settings["confidence"] / 100.0,
    }
