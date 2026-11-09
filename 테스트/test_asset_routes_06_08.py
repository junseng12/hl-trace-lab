import importlib.util
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "01_자산이동_8경로"
sys.path.insert(0, str(ROUTES))


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROUTES / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROUTE06 = load_module("asset_route06", "route06_hyperevm_to_hyperevm.py")
ROUTE07 = load_module("asset_route07", "route07_external_to_hyperevm.py")
ROUTE08 = load_module("asset_route08", "route08_hyperevm_to_external.py")


class Route06Tests(unittest.TestCase):
    def test_native_transfer_exact_hash_match(self):
        sender = "0x" + "11" * 20
        recipient = "0x" + "22" * 20
        bundle = {
            "transaction": {
                "hash": "0x" + "aa" * 32,
                "from": sender,
                "to": recipient,
                "value": hex(2 * 10**15),
                "blockNumber": "0x2a",
            },
            "receipt": {"status": "0x1", "logs": []},
            "block": {"number": "0x2a", "timestamp": "0x64"},
            "blockTimeMs": 100_000,
        }
        result = ROUTE06.analyze_native_transfer(
            bundle,
            expected_from=sender,
            expected_to=recipient,
            expected_amount=Decimal("0.002"),
        )
        self.assertEqual(result["classification"], "EXACT_HASH_MATCH")
        self.assertEqual(result["transfer"]["amount"], "0.002")

    def test_erc20_transfer_requires_one_matching_log(self):
        token = "0x" + "99" * 20
        sender = "0x" + "11" * 20
        recipient = "0x" + "22" * 20
        topic = "0x" + "0" * 24 + sender[2:]
        topic_to = "0x" + "0" * 24 + recipient[2:]
        bundle = {
            "transaction": {"hash": "0x" + "bb" * 32},
            "receipt": {
                "status": "0x1",
                "logs": [{
                    "address": token,
                    "topics": [ROUTE06.parse_erc20_transfers.__globals__["TRANSFER_TOPIC0"], topic, topic_to],
                    "data": hex(15_000_000),
                    "transactionHash": "0x" + "bb" * 32,
                    "logIndex": "0x0",
                }],
            },
        }
        result = ROUTE06.analyze_erc20_transfer(
            bundle,
            token_address=token,
            token_decimals=6,
            expected_from=sender,
            expected_to=recipient,
            expected_amount=Decimal("15"),
        )
        self.assertEqual(result["classification"], "EXACT_HASH_MATCH")
        self.assertEqual(result["candidateCount"], 1)


class Route07Tests(unittest.TestCase):
    def test_op_return_identifier_links_to_evm_input(self):
        identifier = "74f9c9f78e05c163dfb8a903396316f1ed7279acce9b8527b220e7bb77863e3a"
        bitcoin_tx = {
            "status": {"block_time": 1_000},
            "vout": [{
                "scriptpubkey_type": "op_return",
                "scriptpubkey": "6a20" + identifier,
                "scriptpubkey_asm": "OP_RETURN OP_PUSHBYTES_32 " + identifier,
            }],
        }
        evm_bundle = {
            "transaction": {"input": "0xdeadbeef" + identifier + "00"},
            "receipt": {"status": "0x1"},
            "blockTimeMs": 1_036_000,
        }
        result = ROUTE07.analyze_link(bitcoin_tx, evm_bundle)
        self.assertEqual(result["classification"], "EXACT_PROTOCOL_ID_MATCH")
        self.assertEqual(result["observedDelaySeconds"], 36)
        self.assertEqual(result["sharedProtocolIdentifiers"], ["0x" + identifier])

    def test_ascii_lifi_envelope_inside_larger_op_return_push(self):
        identifier = "74f9c9f78e05c163dfb8a903396316f1ed7279acce9b8527b220e7bb77863e3a"
        payload = ("0x" + identifier + "=|lifi").encode("ascii") + b"\x1d\x80\xef\x59"
        script = "6a4c" + f"{len(payload):02x}" + payload.hex()
        bitcoin_tx = {"vout": [{
            "scriptpubkey_type": "op_return",
            "scriptpubkey": script,
        }]}
        self.assertEqual(
            ROUTE07.extract_op_return_identifiers(bitcoin_tx),
            ["0x" + identifier],
        )

    def test_missing_shared_identifier_is_not_found(self):
        identifier = "11" * 32
        bitcoin_tx = {"vout": [{"scriptpubkey": "6a20" + identifier}]}
        evm_bundle = {
            "transaction": {"input": "0x" + "22" * 32},
            "receipt": {"status": "0x1"},
        }
        result = ROUTE07.analyze_link(bitcoin_tx, evm_bundle)
        self.assertEqual(result["classification"], "NOT_FOUND")


class Route08Tests(unittest.TestCase):
    def test_lifi_status_exact_protocol_id_match(self):
        source = "0x" + "aa" * 32
        destination = "0x" + "bb" * 32
        transaction_id = "0x" + "cc" * 32
        payload = {
            "transactionId": transaction_id,
            "status": "DONE",
            "substatus": "COMPLETED",
            "tool": "polymerStandard",
            "sending": {"txHash": source, "chainId": 999, "timestamp": 100},
            "receiving": {"txHash": destination, "chainId": 8453, "timestamp": 111},
        }
        result = ROUTE08.analyze_lifi_status(
            payload,
            source_hash=source,
            from_chain=999,
            to_chain=8453,
            expected_destination_hash=destination,
        )
        self.assertEqual(result["classification"], "EXACT_PROTOCOL_ID_MATCH")
        self.assertEqual(result["transactionId"], transaction_id)
        self.assertEqual(result["observedDelaySeconds"], 11)

    def test_lifi_wrong_destination_is_mismatch(self):
        source = "0x" + "aa" * 32
        payload = {
            "transactionId": "0x" + "cc" * 32,
            "status": "DONE",
            "substatus": "COMPLETED",
            "sending": {"txHash": source, "chainId": 999},
            "receiving": {"txHash": "0x" + "bb" * 32, "chainId": 8453},
        }
        result = ROUTE08.analyze_lifi_status(
            payload,
            source_hash=source,
            from_chain=999,
            to_chain=8453,
            expected_destination_hash="0x" + "dd" * 32,
        )
        self.assertEqual(result["classification"], "MISMATCH")


class VerifiedExamplesTests(unittest.TestCase):
    def test_examples_cover_all_eight_routes(self):
        payload = json.loads((ROUTES / "examples" / "verified_cases.json").read_text(encoding="utf-8"))
        self.assertEqual({row["route"] for row in payload["routes"]}, set(range(1, 9)))
        self.assertFalse(payload["signedActions"])


if __name__ == "__main__":
    unittest.main()
