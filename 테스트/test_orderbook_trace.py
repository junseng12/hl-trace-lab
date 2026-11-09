import importlib.util
import unittest
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "02_오더북_가치이전" / "mainnet_live_trace.py"
SPEC = importlib.util.spec_from_file_location("mainnet_live_trace", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class OrderbookTraceTests(unittest.TestCase):
    def test_fill_key(self):
        row = {"coin": "@107", "time": 10, "tid": 20}
        self.assertEqual(MODULE.fill_key(row), ("@107", "10", "20"))

    def test_counterparty(self):
        own = "0x" + "11" * 20
        other = "0x" + "22" * 20
        trade = {"users": [other, own]}
        self.assertEqual(MODULE.counterparty_from_trade(trade, own), other)

    def test_unrelated_trade_has_no_counterparty(self):
        own = "0x" + "11" * 20
        trade = {"users": ["0x" + "22" * 20, "0x" + "33" * 20]}
        self.assertIsNone(MODULE.counterparty_from_trade(trade, own))


if __name__ == "__main__":
    unittest.main()
