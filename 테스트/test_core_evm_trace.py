import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "01_자산이동_8경로" / "core_evm.py"
SPEC = importlib.util.spec_from_file_location("trace_core_evm", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CoreEvmTraceTests(unittest.TestCase):
    def test_core_to_evm_filter(self):
        user = "0x" + "11" * 20
        rows = [{
            "time": 1234,
            "hash": "0x" + "ab" * 32,
            "delta": {
                "type": "send", "user": user,
                "destination": MODULE.HYPE_SYSTEM_ADDRESS,
                "token": "HYPE", "amount": "0.005",
            },
        }]
        self.assertEqual(
            MODULE.core_to_evm_updates(rows, user, Decimal("0.005"), None), rows
        )

    def test_evm_to_core_filter(self):
        user = "0x" + "22" * 20
        rows = [{
            "time": 5678,
            "delta": {
                "type": "spotTransfer", "user": MODULE.HYPE_SYSTEM_ADDRESS,
                "destination": user, "token": "HYPE", "amount": "0.001",
            },
        }]
        self.assertEqual(
            MODULE.evm_to_core_updates(rows, user, Decimal("0.001")), rows
        )

    def test_wrong_amount_does_not_match(self):
        user = "0x" + "33" * 20
        rows = [{"delta": {
            "type": "send", "user": user,
            "destination": MODULE.HYPE_SYSTEM_ADDRESS,
            "token": "HYPE", "amount": "1",
        }}]
        self.assertEqual(
            MODULE.core_to_evm_updates(rows, user, Decimal("2"), None), []
        )


if __name__ == "__main__":
    unittest.main()
