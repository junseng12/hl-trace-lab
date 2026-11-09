import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1] / "01_자산이동_8경로"
sys.path.insert(0, str(MODULE_DIR))


def load(name: str):
    path = MODULE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


COMMON = load("common")
ROUTE1 = load("route01_external_to_core")
ROUTE2 = load("route02_core_to_external")
ROUTE3 = load("route03_core_to_core")


class AssetRoutesOneToThreeTests(unittest.TestCase):
    def setUp(self):
        self.user = "0x" + "11" * 20
        self.destination = "0x" + "22" * 20
        self.bridge = "0x" + "33" * 20
        self.tx_hash = "0x" + "ab" * 32
        self.core_hash = "0x" + "cd" * 32

    def test_parse_erc20_transfer(self):
        topic_address = lambda address: "0x" + "0" * 24 + address[2:]
        receipt = {"logs": [{
            "address": COMMON.ARBITRUM_USDC,
            "topics": [
                COMMON.TRANSFER_TOPIC0,
                topic_address(self.user),
                topic_address(self.bridge),
            ],
            "data": hex(9_800_000),
            "transactionHash": self.tx_hash,
            "logIndex": "0x1",
        }]}
        rows = COMMON.parse_erc20_transfers(
            receipt, COMMON.ARBITRUM_USDC, 6
        )
        self.assertEqual(rows[0]["amount"], "9.8")
        self.assertEqual(rows[0]["from"], self.user)
        self.assertEqual(rows[0]["to"], self.bridge)

    def test_route1_requires_same_hash_and_exact_amount(self):
        transfers = [{
            "from": self.user, "to": self.bridge,
            "amount": "9.8", "transactionHash": self.tx_hash,
        }]
        updates = [{
            "hash": self.tx_hash, "time": 1000,
            "delta": {"type": "deposit", "usdc": "9.8"},
        }]
        rows = ROUTE1.find_deposit_candidates(
            transfers, updates, user=self.user, tx_hash=self.tx_hash,
            amount=Decimal("9.8"), bridge_address=self.bridge,
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["join"]["sameHash"])
        updates[0]["hash"] = "0x" + "ef" * 32
        self.assertEqual(
            ROUTE1.find_deposit_candidates(
                transfers, updates, user=self.user, tx_hash=self.tx_hash,
                amount=Decimal("9.8"), bridge_address=self.bridge,
            ),
            [],
        )

    def test_route2_matches_gross_minus_fee_to_received(self):
        transfers = [{
            "from": "0x" + "00" * 20,
            "to": self.destination,
            "amount": "9.8",
        }]
        updates = [{
            "hash": self.core_hash, "time": 2000,
            "delta": {"type": "withdraw", "usdc": "10", "fee": "0.2"},
        }]
        rows = ROUTE2.find_withdrawal_candidates(
            transfers, updates, destination=self.destination,
            requested_amount=Decimal("10"), core_hash=self.core_hash,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["join"]["netExpectedUsdc"], "9.8")

    def test_route2_does_not_force_ambiguous_match(self):
        transfers = [
            {"to": self.destination, "amount": "9.8"},
            {"to": self.destination, "amount": "9.8"},
        ]
        updates = [{
            "hash": self.core_hash,
            "delta": {"type": "withdraw", "usdc": "10", "fee": "0.2"},
        }]
        rows = ROUTE2.find_withdrawal_candidates(
            transfers, updates, destination=self.destination,
            requested_amount=Decimal("10"), core_hash=self.core_hash,
        )
        self.assertEqual(COMMON.classify_candidate_count(len(rows)), "AMBIGUOUS")

    def test_route3_merges_same_core_event_seen_by_both_users(self):
        update = {
            "hash": self.core_hash,
            "time": 3000,
            "delta": {
                "type": "send",
                "user": self.user,
                "destination": self.destination,
                "token": "HYPE",
                "amount": "0.001",
            },
        }
        rows = ROUTE3.find_core_transfer_candidates(
            [update], [dict(update)], sender=self.user,
            recipient=self.destination, token="HYPE",
            amount=Decimal("0.001"), core_hash=self.core_hash,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["seenIn"], ["senderLedger", "recipientLedger"]
        )

    def test_route3_rejects_wrong_amount(self):
        update = {
            "hash": self.core_hash,
            "time": 3000,
            "delta": {
                "type": "send", "user": self.user,
                "destination": self.destination,
                "token": "HYPE", "amount": "0.001",
            },
        }
        rows = ROUTE3.find_core_transfer_candidates(
            [update], [], sender=self.user, recipient=self.destination,
            token="HYPE", amount=Decimal("0.002"), core_hash=None,
        )
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
