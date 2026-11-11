import math
import unittest

from zerolife_ai_trader.settings import DEFAULT_SETTINGS, validate_settings


class AITraderSettingsTests(unittest.TestCase):
    def payload(self):
        return dict(DEFAULT_SETTINGS)

    def test_defaults_are_valid_and_normalized(self):
        result = validate_settings(self.payload())
        self.assertEqual(result["symbols"], "SOLUSDC,BTCUSDT,ETHUSDT")
        self.assertEqual(result["strategy"], "ema_rsi")

    def test_legacy_saved_settings_default_paper_auto_execute_to_off(self):
        payload = self.payload()
        payload.pop("paperAutoExecute")
        result = validate_settings(payload)
        self.assertIs(result["paperAutoExecute"], False)

    def test_paper_auto_execute_requires_boolean(self):
        payload = self.payload()
        payload["paperAutoExecute"] = "yes"
        with self.assertRaises(ValueError):
            validate_settings(payload)

    def test_symbols_are_normalized_and_deduplicated(self):
        payload = self.payload()
        payload["symbols"] = " solusdc, BTCUSDT,solusdc "
        self.assertEqual(validate_settings(payload)["symbols"], "SOLUSDC,BTCUSDT")

    def test_rejects_unknown_fields_and_missing_fields(self):
        payload = self.payload()
        payload["unexpected"] = True
        with self.assertRaises(ValueError):
            validate_settings(payload)
        with self.assertRaises(ValueError):
            validate_settings({})

    def test_rejects_unsupported_strategy_source_timeframe_and_pair(self):
        for key, value in (
            ("strategy", "live_market_maker"),
            ("source", "private_exchange_api"),
            ("timeframe", "2m"),
            ("symbols", "SCAMCOINUSDC"),
        ):
            payload = self.payload()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_settings(payload)

    def test_rejects_nonfinite_boolean_and_out_of_range_numbers(self):
        for key, value in (
            ("capital", math.nan),
            ("capital", math.inf),
            ("orderRisk", True),
            ("dailyLoss", 0),
            ("maxPositions", True),
            ("confidence", 101),
            ("fee", 1001),
            ("slippage", -1),
        ):
            payload = self.payload()
            payload[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_settings(payload)

    def test_rejects_non_boolean_notification_values(self):
        payload = self.payload()
        payload["notifySignals"] = "yes"
        with self.assertRaises(ValueError):
            validate_settings(payload)

    def test_rejects_too_many_symbols(self):
        payload = self.payload()
        payload["symbols"] = ",".join(["SOLUSDC"] * 11)
        with self.assertRaises(ValueError):
            validate_settings(payload)


if __name__ == "__main__":
    unittest.main()
